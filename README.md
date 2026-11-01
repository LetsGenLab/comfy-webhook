# ComfyUI Webhook

A ComfyUI node that sends an HTTP webhook when a job succeeds, fails, or is
interrupted. Configure its destination in the workflow or API request.

## Install

Place this repository as one directory inside `ComfyUI/custom_nodes/`. Keep the
root `__init__.py` and the `extension` directory together, then restart ComfyUI
once to load the custom node.

## Use the node

Add **Webhook** from the `webhook` category to the canvas and enter an HTTP or
HTTPS URL. It is a standalone output node and does not need a video connection.
When the entire job finishes, the webhook includes output file descriptors,
including video descriptors if ComfyUI reports them. The extension reads the
submitted node configuration independently of node execution, so a failure in
an earlier node can also produce a webhook. Use one Webhook node per workflow
and enter the URL directly as text; linked inputs are not supported.

The URL is stored in the workflow and in the local delivery queue. Avoid sharing
workflow files containing URLs that you want to keep private.

The node also has an **Auto clean once done** switch, off by default. When it is
on, the plugin waits for a successful webhook response before removing this
job's unchanged output files. Cached outputs, changed or replaced files,
symlinks, unsafe paths, and user input files are preserved. Make sure your
receiver has copied any output it needs before returning HTTP 2xx. Turning the
switch off preserves all outputs. If delivery fails, files are kept and the
same event is retried; if cleanup fails after acknowledgement, cleanup is
retried without sending another webhook.

## API submissions

Set the Webhook node's `url` in the API workflow prompt, or set
`extra_data.webhook_url` for a particular submission:

```json
{
  "prompt": {},
  "extra_data": { "webhook_url": "https://example.com/another-webhook" }
}
```

Replace the example `prompt` with the workflow API prompt. The `extra_data` URL
takes precedence over the node, followed by the optional default URL. Without a
URL, that job produces no webhook. Any HTTP or HTTPS destination is supported;
there is no domain allowlist.
The ComfyUI API should be accessible only to trusted users, since a submitted
job can choose where its webhook is sent. Redirects are not followed.

The webhook covers jobs that ComfyUI accepts into its execution queue. A prompt
rejected before it is queued is reported by the submission API, not the webhook.

For API workflows, enable the node switch with a literal boolean:

```json
"webhook": {
  "class_type": "ComfyWebhook",
  "inputs": { "url": "https://example.com/webhook", "auto_clean_once_done": true }
}
```

Leave the field out or set it to `false` to retain the output files.

An editable FastH3 image-reference workflow is available in
[`workflows/`](workflows/README.md). Model weights and compatible ComfyUI nodes
must still be installed on each destination machine.

## Payload

The extension sends a JSON `POST`. A successful event looks like this:

```json
{
  "version": 1,
  "deliveryId": "job-id",
  "promptId": "job-id",
  "status": "succeeded",
  "outputs": [{ "nodeId": "9", "group": "images", "filename": "result.png", "subfolder": "", "type": "output" }]
}
```

`status` is `succeeded`, `failed`, or `interrupted`. Failure events may include
an `error` object with node and exception type identifiers. Outputs contain at
most 32 file descriptors; the files themselves are not sent and are only
deleted when auto clean is enabled. Prompts,
input values, and exception messages are not included.

Any HTTP 2xx response acknowledges delivery. Other responses and network
errors are retried indefinitely with exponential backoff, including after a
restart. A receiver may get the same event more than once; use `deliveryId` to
handle duplicates. The outbox contains event data and destination URLs, so keep
it private and do not commit it.

## Retry settings

The delivery queue defaults to `comfy-webhook/outbox.sqlite` inside the ComfyUI
user directory. Set `COMFY_WEBHOOK_OUTBOX_PATH` to an absolute path to use a
different location; create its parent directory first. Use a private persistent
volume if events must survive machine replacement. On POSIX, the outbox file
must be owner-only. On Windows, restrict the output and outbox directories
using Windows ACLs; POSIX file-mode checks do not validate Windows ACLs.

Optionally set `COMFY_WEBHOOK_URL` as the default URL for jobs without a node
or per-job URL. The following optional environment variables are in seconds:

| Variable | Default | Meaning |
| --- | --- | --- |
| `COMFY_WEBHOOK_RETRY_INITIAL` | `1` | Initial delay between attempts |
| `COMFY_WEBHOOK_RETRY_MAX` | `30` | Maximum delay between attempts |
| `COMFY_WEBHOOK_TIMEOUT` | `120` | HTTP request timeout |

Values must be positive; the maximum delay must be at least the initial delay.
The callback is delivered asynchronously after the event is saved; failure to
save an event is logged and does not stop ComfyUI from completing the job.

## Optional managed delivery protocol

For an integrating service that requires
explicit storage and cleanup acknowledgement, an optional managed observer
can be enabled with the following environment variables before starting ComfyUI:

| Variable | Purpose |
| --- | --- |
| `COMFY_WEBHOOK_MANAGED_OUTBOX_PATH` | Absolute SQLite path on a private persistent volume; create its parent directory first. Setting this enables managed mode. |
| `COMFY_WEBHOOK_MANAGED_CALLBACKS` | Comma-separated exact HTTPS callback URLs allowed for managed jobs; required in managed mode. |
| `COMFY_WEBHOOK_MANAGED_CALLBACK_FIELD` | Per-job `extra_data` key, default `managed_webhook_url`. Must differ from the generic `webhook_url`. |
| `COMFY_WEBHOOK_MANAGED_API_PREFIX` | One path segment for capability and recovery endpoints, default `/webhook-managed`. |
| `COMFY_WEBHOOK_MANAGED_INPUT_PREFIX` | Prefix for owned inputs, default `comfy-input`. |
| `COMFY_WEBHOOK_MANAGED_OUTPUT_PREFIX` | Prefix for owned MP4 outputs, default `comfy`. |
| `COMFY_WEBHOOK_MANAGED_SIGNING_KEY` | Optional secret, 32–1024 UTF-8 bytes, used to authenticate managed callbacks. Store it privately and share it only with the receiver. |

The protocol requires a UUID prompt ID and top-level input names shaped like
`<input-prefix>-<uuid>-<token>.png` (also `.jpg`, `.jpeg`, or `.webp`), and
one top-level output named `<output-prefix>-<uuid>_<token>.mp4`. Only the
allowlisted per-job callback receives events; a job with no allowed callback
is not enrolled. Managed delivery persists terminal events and retries until
the receiver confirms the terminal result. After that confirmation it removes
only the task-owned files and sends a `cleaned` event when the receiver requires
it. Recovery endpoints expose pending events and acknowledgement by exact UUID.
Unexpected acknowledgement bodies and network errors remain pending.

The configured API prefix exposes `/capabilities` and `/outbox/{prompt_id}`
plus POST/DELETE acknowledgement endpoints. These endpoints inherit the
ComfyUI server's access controls; they have no independent authentication.
Restrict the API to trusted clients. Do not enable the ordinary Webhook node
for the same managed task unless two notifications are explicitly intended.
Managed and generic outboxes are independent; preserve the managed outbox and
files while a callback or cleanup is unresolved. A failure to persist an event
is logged and does not prevent ComfyUI from finishing, so persistent storage
and monitoring remain necessary.
The authenticated server can also receive `POST /staged/{prompt_id}/cleanup`
with `{"files":[...]}` under the configured API prefix, for task-owned inputs
uploaded before a definitively rejected prompt. It refuses cleanup while that
prompt is queued, running, or has an unacknowledged managed event. Call it only
after the receiver knows the graph was not accepted; an ambiguous submission
must remain unresolved.
This endpoint requires the managed signing key and the same
`X-Comfy-Webhook-Signature` HMAC over the exact request body; unsigned requests
are rejected even when the server itself is otherwise reachable.
Managed jobs use the acknowledgement-and-cleanup protocol independently of the
ordinary node's switch; they do not need a Webhook node in the graph.

When a signing key is configured, each managed completion and `cleaned` request
contains `X-Comfy-Webhook-Signature: sha256=<hex>`, where the value is HMAC-SHA256
over the exact request body bytes. The receiver must compare signatures in
constant time and deduplicate by prompt ID and event contents. Retries send the
same event again, so a valid signature alone does not prevent replay. With no
key configured, the header is omitted for compatibility. The capability
response includes `callback_signature: 1` only when signing is configured.
In signed mode it also reports the managed callback field, filename prefixes,
and SHA-256 hashes of allowed callback URLs. A receiver can compare these
before assigning work, without exposing callback URLs in the capability response.

## Test

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```
