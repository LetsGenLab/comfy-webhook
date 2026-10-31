"""ComfyUI webhook nodes and delivery implementation."""

from .nodes import Webhook

NODE_CLASS_MAPPINGS = {"ComfyWebhook": Webhook}
NODE_DISPLAY_NAME_MAPPINGS = {"ComfyWebhook": "Webhook"}
