# ComfyUI Webhook

A ComfyUI node that sends an HTTP webhook when a job succeeds, fails, or is
interrupted. Configure its destination in the workflow or API request.

## Install

Clone this repository as one directory inside `ComfyUI/custom_nodes/`, then
restart ComfyUI once to load the custom node:

```sh
git clone https://github.com/LetsGenLab/comfy-webhook.git ComfyUI/custom_nodes/comfy-webhook
```

Keep the root `__init__.py` and the `extension` directory together. Update the
existing directory when upgrading; do not install a second copy.

For machines whose startup environment is recreated, settings may instead be
stored in `comfy-webhook/settings.json` beneath ComfyUI's user directory. The
file is a JSON object whose keys are `COMFY_WEBHOOK_` environment-variable
names and whose values are strings. It must be owner-only on POSIX. Place it
on a private persistent volume, outside the Git checkout; environment
variables override file settings. Never commit the file or its contents.

## Protect the server

Set `COMFY_WEBHOOK_API_TOKEN` to a private, randomly generated value of at
least 32 UTF-8 bytes before starting ComfyUI to require authentication for the
entire web server, including workflow submission, output download, WebSocket and
managed recovery. API clients send `Authorization: Bearer <token>`. A browser
can sign in using HTTP Basic authentication with username `comfy` and the token
as its password. Use HTTPS; do not put the token in a URL, workflow, log or Git
repository. Without the variable, the plugin does not change server access.

For browser access, set `COMFY_WEBHOOK_ALLOWED_ORIGIN` to the exact public
HTTPS origin, without a trailing slash. With a token enabled, requests that
carry any other `Origin` are rejected, even if ComfyUI's CORS settings are
permissive. If this setting is absent, all requests carrying an `Origin`
header are denied. API clients that omit `Origin` are unaffected.

Custom-node import failures can prevent this middleware from loading while
ComfyUI continues to run. Before using a machine, verify that an unauthenticated
request is rejected and an authenticated request succeeds; use a private
network or platform access control as an additional protection.

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

## Managed delivery protocol

For an integrating service that requires explicit storage and cleanup
acknowledgement, the same plugin can run a global managed observer. It does not
require a Webhook node in the workflow. Enable it with these environment
variables before starting ComfyUI:

| Variable | Purpose |
| --- | --- |
| `COMFY_WEBHOOK_MANAGED_ENABLED` | Set to `1` to start the observer with the defaults below. |
| `COMFY_WEBHOOK_MANAGED_CALLBACKS` | Comma-separated exact HTTPS callback URLs allowed for managed jobs. Required. |
| `COMFY_WEBHOOK_MANAGED_SIGNING_KEY` | Optional secret, 32–1024 UTF-8 bytes, for signed callbacks. Store privately and share only with the receiver. |
| `COMFY_WEBHOOK_MANAGED_SIGNING_KEYS` | Optional JSON object mapping every allowlisted callback URL to its own 32–1024 byte secret. Use instead of the single signing key when callbacks have different receivers. |
| `COMFY_WEBHOOK_MANAGED_OUTBOX_PATH` | Optional absolute SQLite path on a private persistent volume; create its parent directory first. Setting this also enables managed mode. |
| `COMFY_WEBHOOK_MANAGED_CALLBACK_FIELD` | Per-job `extra_data` key, default `letsgen_callback_url`. |
| `COMFY_WEBHOOK_MANAGED_API_PREFIX` | One path segment for capability and recovery endpoints, default `/letsgen`. |
| `COMFY_WEBHOOK_MANAGED_INPUT_PREFIX` | Owned input prefix, default `letsgen-input`. |
| `COMFY_WEBHOOK_MANAGED_OUTPUT_PREFIX` | Owned MP4 output prefix, default `letsgen`. |

When enabled using `COMFY_WEBHOOK_MANAGED_ENABLED`, the default outbox is
`comfy-webhook/managed-outbox.sqlite` inside the ComfyUI user directory. The
user directory must be on persistent storage for callbacks to survive machine
replacement. An API job supplies an allowlisted callback in
`extra_data.letsgen_callback_url`. No callback address or credential is stored
in this repository. Protect the entire ComfyUI API with authentication or a
private network; the plugin does not add access control to ComfyUI. Callback
signing also requires verification by the receiving service.

For multiple receivers, the per-callback signing map must contain exactly the
allowlisted URLs. Store the JSON map as a string in the owner-only settings
file, remove the single-key setting, and retain every key needed by pending
outbox events. Startup rejects an incomplete map or pending callback without a
key. Staged-input cleanup accepts a valid signature from any configured key;
the shared machine API credential and task-specific filename checks still apply.

For backwards compatibility, enabling managed mode with an explicit outbox
path retains its historical defaults (`managed_webhook_url`, `/webhook-managed`,
`comfy-input`, `comfy`). Set the corresponding variables explicitly if using
that mode with Lets Gen.

The protocol requires a UUID prompt ID and top-level input names shaped like
`<input-prefix>-<uuid>-<token>.png` (also `.jpg`, `.jpeg`, or `.webp`), and
one top-level output named `<output-prefix>-<uuid>_<token>.mp4`. A job with no
allowlisted per-job callback is not enrolled. Managed delivery persists
terminal events and retries until the receiver confirms the terminal result.
After that confirmation it removes only the task-owned files and sends a `cleaned` event when the receiver requires
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
