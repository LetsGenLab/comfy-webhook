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
most 32 file descriptors; the files themselves are not sent or deleted. Prompts,
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
volume if events must survive machine replacement. The file is owner-only.

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

## Test

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```
