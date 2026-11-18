#!/usr/bin/env python3
"""Install a pinned comfy-webhook version before starting ComfyUI."""

import fcntl
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


REPOSITORY = "https://github.com/LetsGenLab/comfy-webhook.git"
TOKEN = "COMFY_WEBHOOK_API_TOKEN"
PASSWORD = "COMFY_WEBHOOK_BASIC_PASSWORD"
MAX_SETTINGS_SIZE = 16_384


def prepare_settings(path):
    settings = {}
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > MAX_SETTINGS_SIZE):
            raise ValueError("Existing webhook settings must be an owner-only regular file")
        try:
            settings = json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            raise ValueError("Existing webhook settings must contain valid JSON") from None
        if not isinstance(settings, dict) or any(
            not key.startswith("COMFY_WEBHOOK_") or not isinstance(value, str)
            for key, value in settings.items()
        ):
            raise ValueError("Existing webhook settings must contain COMFY_WEBHOOK_ string values")
    settings.update({key: value for key, value in os.environ.items() if key.startswith("COMFY_WEBHOOK_")})
    if TOKEN not in settings:
        settings[TOKEN] = secrets.token_urlsafe(32)
    for name, minimum in ((TOKEN, 32), (PASSWORD, 1)):
        if name in settings:
            value = settings[name]
            if not minimum <= len(value.encode()) <= 1024 or "\r" in value or "\n" in value:
                raise ValueError(f"{name} must contain {minimum}–1024 UTF-8 bytes on one line")
    payload = (json.dumps(settings, indent=2, sort_keys=True) + "\n").encode()
    if len(payload) > MAX_SETTINGS_SIZE:
        raise ValueError("Webhook settings exceed 16 KiB")
    return payload


def write_settings(path, payload):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(payload)
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def git(directory, *args):
    return subprocess.check_output(
        ["git", "-C", str(directory), *args], text=True, stdin=subprocess.DEVNULL,
        timeout=90, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).strip()


def install_plugin(custom_nodes, revision):
    destination = custom_nodes / "comfy-webhook"
    if destination.is_symlink():
        raise ValueError("Existing comfy-webhook path is a symlink")
    if destination.exists():
        if (not (destination / ".git").is_dir()
                or git(destination, "config", "--local", "--get", "remote.origin.url") != REPOSITORY
                or git(destination, "status", "--porcelain")):
            raise ValueError("Existing comfy-webhook installation must be a clean checkout from the official repository")
        if git(destination, "rev-parse", "HEAD") != revision:
            git(destination, "fetch", "--depth=1", "origin", revision)
            git(destination, "checkout", "--detach", revision)
        return

    # Stage outside custom_nodes so a failed download cannot leave a partial plugin.
    with tempfile.TemporaryDirectory(dir=custom_nodes.parent) as directory:
        git(directory, "init", "-q")
        git(directory, "remote", "add", "origin", REPOSITORY)
        git(directory, "fetch", "--depth=1", "origin", revision)
        git(directory, "checkout", "--detach", revision)
        Path(directory).rename(destination)


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"[0-9a-f]{40}", sys.argv[1]):
        raise ValueError("Usage: python3 install.py <full comfy-webhook commit SHA>")
    root = Path(os.environ.get("COMFYUI_DIR", ".")).resolve()
    custom_nodes = root / "custom_nodes"
    if not (root / "main.py").is_file() or not (root / "folder_paths.py").is_file() or not custom_nodes.is_dir():
        raise ValueError("Set COMFYUI_DIR to the ComfyUI installation directory")
    user_dir = (root / os.environ.get("COMFYUI_USER_DIR", "user")).resolve()
    path = user_dir / "comfy-webhook" / "settings.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_uid != os.geteuid() or path.parent.stat().st_mode & 0o077:
        raise ValueError("Webhook settings directory must be owned by the ComfyUI user and have mode 0700")
    with path.parent.joinpath("install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        payload = prepare_settings(path)
        settings = json.loads(payload)
        managed = (settings.get("COMFY_WEBHOOK_MANAGED_ENABLED", "").strip() == "1"
                   or settings.get("COMFY_WEBHOOK_MANAGED_OUTBOX_PATH", "").strip())
        if managed and (custom_nodes / "letsgen_completion").exists():
            raise ValueError("Move the legacy letsgen_completion observer outside custom_nodes before enabling managed delivery")
        if managed and not settings.get("COMFY_WEBHOOK_MANAGED_CALLBACKS", "").strip():
            raise ValueError("Managed delivery requires COMFY_WEBHOOK_MANAGED_CALLBACKS")
        install_plugin(custom_nodes, sys.argv[1])
        write_settings(path, payload)
    print(f"Installed comfy-webhook at {sys.argv[1]}. Private settings: {path}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"comfy-webhook install: {error}")
