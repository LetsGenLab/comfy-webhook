"""Observe reviewed ComfyUI queue completion without changing workflow graphs."""

import logging
import os
import re

LOG = logging.getLogger("comfy-webhook-managed")


def owned_input_files(prompt_id, graph, input_prefix):
    """Collect only this API job's uniquely named top-level uploads."""
    if not isinstance(prompt_id, str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", prompt_id) or not isinstance(graph, dict):
        return []
    pattern = re.compile(re.escape(input_prefix) + r"-" + re.escape(prompt_id) + r"-[A-Za-z0-9_-]{1,100}\.(?:png|jpg|jpeg|webp)")
    names = set()
    for node in graph.values():
        if not isinstance(node, dict) or node.get("class_type") != "LoadImage" or not isinstance(node.get("inputs"), dict):
            continue
        name = node["inputs"].get("image")
        if isinstance(name, str) and pattern.fullmatch(name):
            names.add(name)
    return sorted(names) if len(names) <= 9 else []


def event_from_completion(prompt_id, status, history, output_prefix):
    if not isinstance(prompt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prompt_id):
        raise ValueError("Invalid prompt identity")
    if status is None or status.status_str not in ("success", "error"):
        raise ValueError("Unknown ComfyUI completion status")
    messages = status.messages or []
    interrupted = any(isinstance(item, (list, tuple)) and item and item[0] == "execution_interrupted" for item in messages)
    errored = any(isinstance(item, (list, tuple)) and item and item[0] == "execution_error" for item in messages)
    kind = "interrupted" if interrupted else "failed" if errored or status.status_str == "error" else "succeeded"
    event = {"version": 1, "deliveryId": prompt_id, "promptId": prompt_id,
             "status": kind, "outputs": []}
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
    # Keep the provider's own outputs ahead of previews. ComfyUI can report
    # many intermediate files before the final video, and the receiver needs
    # the video descriptor to persist it before acknowledging cleanup.
    priority_files, other_files = [], []
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
                    item = {"nodeId": str(node_id), "group": str(group),
                            "filename": name, "subfolder": subfolder, "type": location}
                    target = priority_files if name.startswith(f"{output_prefix}-{prompt_id}_") and name.endswith(".mp4") else other_files
                    if len(target) < 32:
                        target.append(item)
    event["outputs"] = (priority_files + other_files)[:32]
    return event


def install_queue_hook(queue, outbox, callback_field, allowed_callbacks, input_prefix, output_prefix):
    """Wrap just this queue instance; never block a GPU worker on HTTP."""
    if getattr(queue, "_comfy_managed_completion_hook", False):
        raise RuntimeError("ComfyUI completion hook already installed")
    original = queue.task_done

    def task_done(item_id, history_result, status, process_item=None):
        with queue.mutex:
            item = queue.currently_running.get(item_id)
            prompt_id = item[1] if item else None
            extra = item[3] if item and len(item) > 3 and isinstance(item[3], dict) else {}
            callback = extra.get(callback_field)
            callback = callback if isinstance(callback, str) and callback in allowed_callbacks else None
            graph = item[2] if item and len(item) > 2 else None
            inputs = owned_input_files(prompt_id, graph, input_prefix) if callback else []
        stored, event = False, None
        try:
            event = event_from_completion(prompt_id, status, history_result, output_prefix)
            stored = outbox.add(event, callback, inputs)
        except Exception:
            LOG.exception("Could not persist ComfyUI completion event")
        # The executor already finished and produced history_result. Commit the
        # callback before ComfyUI removes the running item, so a crash during
        # queue completion cannot discard an event already available here.
        # Outbox failures must not break ComfyUI's own queue completion.
        result = original(item_id, history_result, status, process_item=process_item)
        # A very fast callback can be acknowledged before ComfyUI inserts its
        # history entry. Remove that entry after insertion in this one race.
        if stored and event and event["status"] == "succeeded":
            try:
                if outbox.pending_event(prompt_id) is None:
                    queue.delete_history_item(prompt_id)
            except Exception:
                LOG.warning("Could not clear acknowledged ComfyUI history")
        return result

    queue.task_done = task_done
    queue._comfy_managed_completion_hook = True
