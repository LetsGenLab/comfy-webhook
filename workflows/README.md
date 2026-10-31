# FastH3 image-reference workflow

`fasth3-omniref-image-silent.json` is an editable ComfyUI workflow, adapted
from the [official FastH3 OmniRef template](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_fasth3_ominiref_r2v.json).
It starts at approximately 480p and five seconds, accepts an image reference,
and saves video without an audio track. The audio VAE remains connected to the
model's conditioning node; it is not connected to the saved video.

Install this repository in `ComfyUI/custom_nodes`, restart ComfyUI, and import
the JSON through ComfyUI. The Webhook node's URL is deliberately empty: set it
to your own receiver if you want notifications. It observes the whole queued
job, including failures, and does not need a connection to the video node.

Install the node classes and model files identified in the workflow's model
metadata and official template. In particular, it uses the FastH3 OmniRef
INT8 ConvRot checkpoint, Qwen3-VL text encoder, video and audio VAEs, and VSA
attention. Availability, ComfyUI version, compatible CUDA kernels, GPU memory,
and file names differ between providers; importing a workflow does not install
those dependencies. The default image is the official template's
`yellow_plane_ref.png`; download that asset from the template's note or select
your own reference image before running.

Do not put private webhook URLs or user inputs in a workflow intended for
sharing. This workflow tests the generic webhook contract; it does not claim
compatibility with an application's separate acknowledgement or cleanup
protocol.
