"""A workflow-level webhook configuration node."""


class Webhook:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"url": ("STRING", {"default": "", "multiline": False})},
                "optional": {"auto_clean_once_done": ("BOOLEAN", {"default": False, "display_name": "Auto clean once done",
                    "tooltip": "Remove unchanged, newly generated output files after the webhook is acknowledged."})}}

    RETURN_TYPES = ()
    FUNCTION = "configure"
    OUTPUT_NODE = True
    CATEGORY = "webhook"
    DESCRIPTION = "Send a webhook when this workflow succeeds, fails, or is interrupted."

    def configure(self, url, auto_clean_once_done=False):
        # The queue observer reads the submitted configuration even if this
        # node has not executed before another node fails.
        return ()
