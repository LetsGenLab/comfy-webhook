"""Private machine settings, kept outside the installable plugin checkout."""

import json
import os
import stat
from pathlib import Path


def load(user_directory, environ=os.environ):
    path = Path(user_directory) / "comfy-webhook" / "settings.json"
    values = {}
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16_384:
            raise ValueError("Invalid webhook settings file")
        if os.name == "posix" and (info.st_mode & 0o077):
            raise ValueError("Webhook settings file must be owner-only")
        with path.open("r", encoding="utf-8") as file:
            values = json.load(file)
        if not isinstance(values, dict) or any(not isinstance(k, str) or not k.startswith("COMFY_WEBHOOK_")
                                                or not isinstance(v, str) for k, v in values.items()):
            raise ValueError("Invalid webhook settings")
    # Explicit environment configuration retains precedence over the file.
    return {**values, **environ}
