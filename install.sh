#!/usr/bin/env bash

main() {
    set +x
    set -eu

    local revision="${WEBHOOK_REVISION:-}"
    if [[ -z "$revision" ]]; then
        revision="$(GIT_TERMINAL_PROMPT=0 git ls-remote --exit-code \
            https://github.com/LetsGenLab/comfy-webhook.git refs/heads/main)"
        revision="${revision%%$'\t'*}"
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
    "${COMFYUI_PYTHON:-python3}" "$installer" "$revision"
}

main "$@"
