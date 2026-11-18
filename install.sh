#!/usr/bin/env bash

main() {
    set +x
    set -eu

    local python="${COMFYUI_PYTHON:-python3}"
    local revision="${WEBHOOK_REVISION:-}"
    if [[ -z "$revision" ]]; then
        revision="$(GIT_TERMINAL_PROMPT=0 "$python" - <<'PY'
import os
import signal
import subprocess

with subprocess.Popen(
    ["git", "ls-remote", "--exit-code", "https://github.com/LetsGenLab/comfy-webhook.git", "refs/heads/main"],
    text=True, stdout=subprocess.PIPE, start_new_session=True,
) as git:
    try:
        stdout, _ = git.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(git.pid, signal.SIGKILL)
        git.communicate()
        raise SystemExit("comfy-webhook install: GitHub did not return the latest commit within 30 seconds")
    if git.returncode:
        raise SystemExit(git.returncode)
    print(stdout.split("\t", 1)[0])
PY
)"
    fi
    if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
        echo "comfy-webhook install: expected a full commit SHA" >&2
        return 1
    fi

    if [[ -n "${COMFY_WEBHOOK_MANAGED_CALLBACKS:-}" ]]; then
        export COMFY_WEBHOOK_MANAGED_ENABLED="${COMFY_WEBHOOK_MANAGED_ENABLED:-1}"
    fi

    installer="$(mktemp)"
    trap 'rm -f "$installer"' EXIT
    curl --fail --silent --show-error --location --connect-timeout 10 --max-time 60 \
        "https://raw.githubusercontent.com/LetsGenLab/comfy-webhook/$revision/install.py" \
        --output "$installer"
    "$python" "$installer" "$revision"
}

main "$@"
