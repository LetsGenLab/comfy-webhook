"""Optional managed delivery with acknowledged, task-scoped cleanup."""

import asyncio
import hashlib
import json
import logging
import os
import re
from urllib.parse import urlsplit

from aiohttp import web

from .events import install_queue_hook
from .outbox import Outbox

LOG = logging.getLogger("comfy-webhook-managed")


def start(server, input_root, output_root, environ=os.environ):
    path = environ.get("COMFY_WEBHOOK_MANAGED_OUTBOX_PATH", "").strip()
    if not path:
        return None
    callback_field = environ.get("COMFY_WEBHOOK_MANAGED_CALLBACK_FIELD", "managed_webhook_url").strip()
    input_prefix = environ.get("COMFY_WEBHOOK_MANAGED_INPUT_PREFIX", "comfy-input").strip()
    output_prefix = environ.get("COMFY_WEBHOOK_MANAGED_OUTPUT_PREFIX", "comfy").strip()
    api_prefix = environ.get("COMFY_WEBHOOK_MANAGED_API_PREFIX", "/webhook-managed").strip()
    callbacks = frozenset(x.strip() for x in environ.get("COMFY_WEBHOOK_MANAGED_CALLBACKS", "").split(",") if x.strip())
    signing_key = environ.get("COMFY_WEBHOOK_MANAGED_SIGNING_KEY")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", callback_field):
        raise ValueError("Invalid managed callback field")
    if callback_field == "webhook_url":
        raise ValueError("Managed and generic callback fields must be distinct")
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,32}", x) for x in (input_prefix, output_prefix)):
        raise ValueError("Invalid managed filename prefix")
    if not re.fullmatch(r"/[A-Za-z0-9_-]{1,64}", api_prefix):
        raise ValueError("Invalid managed API prefix")
    if not callbacks:
        raise ValueError("Managed mode requires callback allowlist")
    for callback in callbacks:
        url = urlsplit(callback)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("Invalid managed callback URL")
    queue = server.prompt_queue
    outbox = Outbox(path, "", output_root, input_root, queue.delete_history_item,
                    input_prefix=input_prefix, output_prefix=output_prefix, signing_key=signing_key)
    install_queue_hook(queue, outbox, callback_field, callbacks, input_prefix, output_prefix)
    stop, thread = outbox.start()

    @server.routes.get(api_prefix + "/capabilities")
    async def capabilities(_request):
        capabilities = {"callback": 1 if thread.is_alive() else 0, "cleanup_ack": 1, "outbox_recovery": 2}
        if signing_key is not None:
            capabilities["callback_signature"] = 1
            capabilities["callback_field"] = callback_field
            capabilities["input_prefix"] = input_prefix
            capabilities["output_prefix"] = output_prefix
            capabilities["callback_url_hashes"] = sorted(hashlib.sha256(url.encode()).hexdigest() for url in callbacks)
            capabilities["staged_cleanup"] = 1
        return web.json_response(capabilities)

    @server.routes.get(api_prefix + "/outbox/{prompt_id}")
    async def pending(request):
        event = await asyncio.to_thread(outbox.pending_event, request.match_info["prompt_id"])
        return web.json_response(event) if event is not None else web.json_response({"error": "not found"}, status=404)

    @server.routes.post(api_prefix + "/outbox/{prompt_id}/ack")
    async def recover(request):
        if request.content_length is not None and request.content_length > 4096:
            return web.json_response({"error": "too large"}, status=413)
        try:
            acknowledgement = await request.json()
            ok = await asyncio.to_thread(outbox.recover_cleanup, request.match_info["prompt_id"], acknowledgement)
        except (ValueError, OSError, json.JSONDecodeError):
            ok = False
        return web.json_response({"status": "cleaned"}) if ok else web.json_response({"error": "not ready"}, status=409)

    @server.routes.delete(api_prefix + "/outbox/{prompt_id}/ack")
    async def forget(request):
        ok = await asyncio.to_thread(outbox.forget_cleaned, request.match_info["prompt_id"])
        return web.json_response({"accepted": True}) if ok else web.json_response({"error": "not ready"}, status=409)

    @server.routes.post(api_prefix + "/staged/{prompt_id}/cleanup")
    async def staged_cleanup(request):
        if request.content_length is not None and request.content_length > 4096:
            return web.json_response({"error": "too large"}, status=413)
        try:
            body = await request.content.read(4097)
            if len(body) > 4096:
                return web.json_response({"error": "too large"}, status=413)
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {"files"}:
                raise ValueError("Invalid request")
            ok = await asyncio.to_thread(outbox.cleanup_staged, request.match_info["prompt_id"], data["files"], queue)
        except (ValueError, OSError, json.JSONDecodeError):
            ok = False
        return web.json_response({"status": "cleaned"}) if ok else web.json_response({"error": "not ready"}, status=409)

    LOG.info("Managed webhook observer enabled")
    return outbox, stop, thread
