"""ComfyUI custom-node entry point for the completion webhook."""

import folder_paths
import os
from server import PromptServer

from .extension import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .extension.bootstrap import start

_outbox, _stop, _thread = start(PromptServer.instance.prompt_queue, folder_paths.get_user_directory(),
                                output_directory=folder_paths.get_output_directory())

# The optional managed protocol has an independent outbox and remains off
# unless explicitly configured. The ordinary webhook node stays unchanged.
from .extension.managed.bootstrap import start as start_managed

_managed = None
if os.environ.get("COMFY_WEBHOOK_MANAGED_OUTBOX_PATH", "").strip():
    _managed = start_managed(PromptServer.instance, folder_paths.get_input_directory(), folder_paths.get_output_directory())
