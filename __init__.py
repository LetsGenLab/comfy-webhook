"""ComfyUI custom-node entry point for the completion webhook."""

import folder_paths
from server import PromptServer

from .extension import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .extension.access import install as install_access_control
from .extension.bootstrap import start
from .extension.settings import load as load_settings

_user_directory = folder_paths.get_user_directory()
_settings = load_settings(_user_directory)
install_access_control(PromptServer.instance, _settings)
_outbox, _stop, _thread = start(PromptServer.instance.prompt_queue, _user_directory, environ=_settings,
                                output_directory=folder_paths.get_output_directory())

# The optional managed observer uses an independent outbox; ordinary webhook
# jobs and their settings remain unchanged.
from .extension.managed.bootstrap import start as start_managed

_managed = start_managed(PromptServer.instance, folder_paths.get_input_directory(), folder_paths.get_output_directory(),
                         environ=_settings, user_directory=_user_directory)
