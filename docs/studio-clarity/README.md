# Studio clarity review captures

Nine comparison sheets from the final council round. Each sheet shows main
(top or left, labelled AFTER) and this branch (bottom or right, labelled NEW).

The captures come from the built app against a real local API in accounts mode,
signed in as the administrator, with a real ROCm worker running SSD-1B +
Lightning, Stable Diffusion 3.5 Medium and Real-ESRGAN Fast. Every image is a
real generation. The live failure was produced by killing a slow simulated
worker twice while it ran a job, so the API failed the job ("worker
disconnected") and the open page received the failure.

| Sheet | Shows |
| --- | --- |
| `desktop-generate.jpg` | Generate without the repeated title and mode switch |
| `desktop-generate-run-strip.jpg` | A job taking the stage while it renders, then its result |
| `desktop-live-failed.jpg` | A failed job: failed tiles, the reason and Try again |
| `desktop-offline-generate.jpg` | No worker connected: one cause and one action |
| `desktop-images-lineage.jpg` | Lineage start position and the floating zoom toolbar |
| `desktop-admin-models.jpg` | Compact model cards with one status line |
| `desktop-metrics-usage.jpg` | Metrics without the shared header, models by name |
| `phone-live-failed.jpg` | A failure brought into view on a phone |
| `phone-upscale.jpg` | One-row phone result toolbar and the output size |
