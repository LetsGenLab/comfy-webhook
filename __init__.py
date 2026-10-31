"""ComfyUI custom-node entry point for the completion webhook."""

import folder_paths
from server import PromptServer

from .extension import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .extension.bootstrap import start

_outbox, _stop, _thread = start(PromptServer.instance.prompt_queue, folder_paths.get_user_directory())
