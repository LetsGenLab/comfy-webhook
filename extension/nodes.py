"""A workflow-level webhook configuration node."""


class Webhook:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"url": ("STRING", {"default": "", "multiline": False})}}

    RETURN_TYPES = ()
    FUNCTION = "configure"
    OUTPUT_NODE = True
    CATEGORY = "webhook"
    DESCRIPTION = "Send a webhook when this workflow succeeds, fails, or is interrupted."

    def configure(self, url):
        # The queue observer reads the submitted configuration even if this
        # node has not executed before another node fails.
        return ()
