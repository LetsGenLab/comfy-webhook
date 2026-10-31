"""Set up a persistent outbox when ComfyUI loads the extension."""

import logging
import os

from .events import install_queue_hook
from .outbox import Outbox

LOG = logging.getLogger("comfy-webhook")


def start(queue, user_directory, environ=os.environ):
    path = environ.get("COMFY_WEBHOOK_OUTBOX_PATH", "").strip()
    if not path:
        directory = os.path.join(os.path.abspath(user_directory), "comfy-webhook")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, "outbox.sqlite")
    callback = environ.get("COMFY_WEBHOOK_URL", "").strip()
    outbox = Outbox(
        path, callback,
        retry_initial=float(environ.get("COMFY_WEBHOOK_RETRY_INITIAL", "1")),
        retry_max=float(environ.get("COMFY_WEBHOOK_RETRY_MAX", "30")),
        timeout=float(environ.get("COMFY_WEBHOOK_TIMEOUT", "120")),
    )
    install_queue_hook(queue, outbox)
    stop, thread = outbox.start()
    LOG.info("ComfyUI webhook enabled")
    return outbox, stop, thread
