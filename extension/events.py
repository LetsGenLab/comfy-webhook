"""Capture terminal queue events without changing workflows."""

import logging
import os
import re

LOG = logging.getLogger("comfy-webhook")


def event_from_completion(prompt_id, status, history):
    if not isinstance(prompt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prompt_id):
        raise ValueError("Invalid prompt identity")
    if status is None or status.status_str not in ("success", "error"):
        raise ValueError("Unknown completion status")
    messages = status.messages or []
    interrupted = any(isinstance(item, (list, tuple)) and item and item[0] == "execution_interrupted" for item in messages)
    failed = any(isinstance(item, (list, tuple)) and item and item[0] == "execution_error" for item in messages)
    kind = "interrupted" if interrupted else "failed" if failed or status.status_str == "error" else "succeeded"
    event = {"version": 1, "deliveryId": prompt_id, "promptId": prompt_id, "status": kind, "outputs": []}
    if kind != "succeeded":
        for item in messages:
            if (isinstance(item, (list, tuple)) and len(item) == 2
                    and item[0] in ("execution_error", "execution_interrupted") and isinstance(item[1], dict)):
                detail = {}
                for source, target in (("node_id", "nodeId"), ("node_type", "nodeType"), ("exception_type", "type")):
                    value = item[1].get(source)
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.: -]{1,128}", value):
                        detail[target] = value
                event["error"] = detail
                break
    outputs = history.get("outputs", {}) if isinstance(history, dict) else {}
    if kind == "succeeded" and isinstance(outputs, dict):
        for node_id, output in outputs.items():
            if not isinstance(output, dict):
                continue
            for group, entries in output.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    name, subfolder, location = entry.get("filename"), entry.get("subfolder", ""), entry.get("type")
                    if (not isinstance(name, str) or not name or name in (".", "..")
                            or os.path.basename(name) != name or "\\" in name
                            or not isinstance(subfolder, str) or "\\" in subfolder
                            or any(part in ("..", ".") for part in subfolder.split("/"))
                            or subfolder.startswith("/") or location != "output"):
                        continue
                    event["outputs"].append({"nodeId": str(node_id), "group": str(group),
                                             "filename": name, "subfolder": subfolder, "type": location})
                    if len(event["outputs"]) == 32:
                        return event
    return event


def workflow_callback(graph):
    """Read a literal URL from the submitted graph, even when execution fails."""
    if not isinstance(graph, dict):
        return None
    urls = []
    for node in graph.values():
        if not isinstance(node, dict) or node.get("class_type") != "ComfyWebhook":
            continue
        inputs = node.get("inputs")
        value = inputs.get("url") if isinstance(inputs, dict) else None
        if not isinstance(value, str):
            raise ValueError("Webhook node URL must be a text value")
        if value.strip():
            urls.append(value.strip())
    if len(urls) > 1:
        raise ValueError("Use only one webhook node per workflow")
    return urls[0] if urls else None


def install_queue_hook(queue, outbox):
    if getattr(queue, "_comfy_webhook_hook", False):
        raise RuntimeError("Webhook hook already installed")
    original = queue.task_done

    def task_done(item_id, history_result, status, process_item=None):
        with queue.mutex:
            item = queue.currently_running.get(item_id)
            prompt_id = item[1] if item else None
            graph = item[2] if item and len(item) > 2 else None
            extra = item[3] if item and len(item) > 3 and isinstance(item[3], dict) else {}
            callback = extra.get("webhook_url")
        try:
            if callback is None:
                callback = workflow_callback(graph)
            outbox.add(event_from_completion(prompt_id, status, history_result), callback)
        except Exception:
            LOG.exception("Could not persist completion event")
        return original(item_id, history_result, status, process_item=process_item)

    queue.task_done = task_done
    queue._comfy_webhook_hook = True
