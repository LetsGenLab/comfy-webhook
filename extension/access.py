"""Optional access control for the ComfyUI web server."""

import base64
import binascii
import hashlib
import hmac
import logging
import os

from aiohttp import web


def install(server, environ=os.environ):
    token = environ.get("COMFY_WEBHOOK_API_TOKEN", "")
    if not token:
        return False
    token_bytes = token.encode("utf-8")
    password = environ.get("COMFY_WEBHOOK_BASIC_PASSWORD", token)
    password_bytes = password.encode("utf-8")
    allowed_origin = environ.get("COMFY_WEBHOOK_ALLOWED_ORIGIN", "").strip()
    valid_token = 32 <= len(token_bytes) <= 1024 and "\n" not in token and "\r" not in token
    if not valid_token:
        logging.getLogger("comfy-webhook").error("API token is invalid; all requests are denied")
    expected = hashlib.sha256(token_bytes).digest()
    expected_basic = hashlib.sha256(password_bytes).digest()
    valid_password = 1 <= len(password_bytes) <= 1024 and "\n" not in password and "\r" not in password

    @web.middleware
    async def authenticate(request, handler):
        origin = request.headers.get("Origin")
        # Built frontend assets are immutable files. Their URL can change when
        # a machine image is moved to another host, so do not bind them to a
        # configured browser origin. Authentication still applies below.
        path = getattr(request, "path", "")
        static_asset = (getattr(request, "method", "") in ("GET", "HEAD")
                        and path.startswith("/assets/")
                        and all(part not in (".", "..") for part in path.split("/")))
        if origin is not None and not static_asset and (not allowed_origin or origin != allowed_origin):
            return web.Response(status=403)
        header = request.headers.get("Authorization", "")
        candidate = b""
        expected_digest = expected
        if len(header) <= 4096 and header.startswith("Bearer "):
            candidate = header[7:].encode("utf-8")
        elif len(header) <= 4096 and header.startswith("Basic "):
            expected_digest = expected_basic
            try:
                value = base64.b64decode(header[6:], validate=True)
                name, separator, secret = value.partition(b":")
                if valid_password and separator and name == b"comfy":
                    candidate = secret
            except (ValueError, binascii.Error):
                pass
        if not valid_token or not candidate or not hmac.compare_digest(hashlib.sha256(candidate).digest(), expected_digest):
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="ComfyUI", charset="UTF-8"'})
        return await handler(request)

    server.app.middlewares.append(authenticate)
    return True
