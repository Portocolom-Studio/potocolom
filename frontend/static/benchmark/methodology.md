# RX 7600 XT image-generation benchmark, October 2026

The run finished on 2026-10-28 at 06:42 UTC. It covers 12 models, 36 prompts,
and five parameter variants per model: 2,160 planned cases. It saved 1,837
successful images. There were zero recorded inference failures and 323
capacity skips. A skip can include a stopped attempt. It is not a successful
image, a timed result, or an inference-failure count.

The GPU also drives the display. A monitor stopped a batch when total device
usage crossed the threshold intended to keep 2 GiB free. Remaining cases at
that resolution were skipped rather than retried. Z-Image Turbo, MOSS-Image
SFT and FLUX 3 skipped all 1024px cases. Kromoss completed one 1024px case
before the guard stopped that size. Iris completed all 180 cases, including
both 1024px variants. These outcomes apply to this shared display GPU and
this configuration, not to every 16 GB card.

## Hardware and software

- AMD Radeon RX 7600 XT, gfx1102, 16 GB VRAM; also used for the desktop.
- Sequential generation, one worker. No competing inference worker.
- SDXL/SSD baseline models used full GPU residency. Other models used
  component offload to CPU. No disk offload and no torch.compile.
- Recorded environment: PyTorch 2.9.1+rocm6.3, Diffusers 0.42.0.dev0,
  Transformers 5.13.1, Accelerate 1.14.0, huggingface-hub 1.33.0,
  torchao 0.17.0. See the raw report's run_context for the recorded code
  revisions and environment notes; this is not a dependency lockfile.
- Candidate weights use bfloat16. Manifests specify the other models' dtypes.
  Iris uses its vendored upstream implementation and CPU text encoding when
  offloading. The raw report includes weight revision pins; the manifest and
  adapter source specify the separate encoder and implementation pins.

## What the times mean

gpu_ms is elapsed time around the engine call, not a GPU kernel or denoising
timer. CPU encoding, transfers, and component swaps can be inside this time.
wall_s covers the generation request and output retrieval. Explicit model
loading happens separately. load_ms is the first successful row's reported
load measurement, not the total time spent loading across the run. Offload
and batch restarts can incur more loads.

The table below reports successful wall times by parameter variant. Ranges
are the minimum and maximum across different prompts, not repeated trials of
one identical input. Every variant has at most one measured output per prompt.
Cold starts and shape-specific kernel setup remain in the recorded data.
CPU loader probes overlapped early SD3.5 cases. Cases were grouped by model
and variant rather than interleaved. The normal desktop also used the GPU.

Models use different resolutions, step counts, guidance and memory modes.
Skipped 1024px cases also change the mix behind per-model averages. Aggregate
charts describe this run; they are not a controlled speed or image-quality
ranking. This study has no automated quality score, repeated-trial confidence
interval, or claim of a speed improvement. Use the per-variant table and raw
settings when selecting a configuration.

All 1,837 stored image files decoded and matched their recorded dimensions.
They contain 1,765 distinct file hashes: the 36 guidance-0/guidance-1 pairs
for each of SDXL Fast and Hyper-SD produced identical bytes. Guidance at these
values does not enable classifier-free guidance on those paths. Both planned
cases remain in the data. File checks do not establish image quality.

## Sample images and raw data

The page shows six AI-generated examples: prompt 2, "Studio squirrel
portrait", seed 100, at 512x512, for the six models with that size in their
matrix. These fixed cases were not selected as the best outputs. Steps and
guidance differ by model and are present in results.json. The full prompt
text is in that file's prompts list. The samples are unchanged copies of the
saved image files. The published examples were visually inspected.
They are PNG masters. The original benchmark gave them .webp names without
changing their encoding; the public copies use .png names and unchanged bytes.
The benchmark CLI now names future master downloads .png as well.
The reviewed Iris 512px examples have visible block artefacts. Its reviewed
768px and 1024px examples are cleaner. This is an observation about these
outputs, not a verified diagnosis or a quality score. The Iris adapter uses
bfloat16 weights to fit this card; upstream recommends float32 weights with
bfloat16 autocast. These timings therefore describe this memory-saving
configuration, not a claim of numerical equivalence to upstream inference.

The raw report contains all 2,160 outcomes, prompt texts, parameters, timings,
memory modes and weight pins. It omits local paths, API addresses, account
and job identifiers. Only the six example images are included in the public
site; the other original images remain in the local archive. The previous
published report remains in repository history. Retired models are excluded
from the new active report, while their earlier measurements remain local.

## Licences and commercial use

The platform's AGPL licence does not replace model licences. The report
publishes measurements and selected outputs, not model weights. The source
repository's docs/third-party-models.md records the full component and encoder
licences and notices.

| Models                                 | Weight licence                                        | Commercial use                                                                                                                                                                                                                                       |
| -------------------------------------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| MOSS-Image SFT, Kromoss, Iris-3B       | Apache 2.0                                            | Permitted, including hosting. Retain licences and required notices when distributing; mark changes. No revenue cap.                                                                                                                                  |
| FLUX.2 Klein 4B, Z-Image Turbo, SSD-1B | Apache 2.0                                            | Permitted under the licence and notice duties.                                                                                                                                                                                                       |
| SDXL base, SDXL Fast, SDXL Hyper-SD    | CreativeML Open RAIL++-M base and adapter terms       | Permitted subject to the applicable use restrictions.                                                                                                                                                                                                |
| SSD-1B + Lightning                     | Apache 2.0 base plus CreativeML Open RAIL++-M adapter | Permitted with both sets of terms and the adapter's use restrictions.                                                                                                                                                                                |
| SD3.5 Medium                           | Stability AI Community                                | Revenue threshold and other terms apply; see the upstream licence and registration requirements. Powered by Stability AI.                                                                                                                            |
| FLUX 3 Image Klein                     | FLUX Kommunity v1.0                                   | The commercial-output exception requires gross annualized revenue below USD 5 million, including affiliates. Content filtering or output review and legally required AI disclosures apply. Other commercial rights can require a separate agreement. |

Apache 2.0 does not grant trademark rights or guarantee rights in generated
content. FLUX 3 is not an unrestricted commercial model. Its output exception
does not grant a general right to distribute commercial modified models.
The new candidates remain benchmark-only in the platform; this report does
not enable a production service.

Exact upstream sources for the new weights:

- [MOSS-Image SFT licence](https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT/blob/f972bd4b4b9e3852133cc5da954d87bc32cd8fb9/LICENSE)
- [Kromoss model card](https://huggingface.co/lodestones/Kromoss/blob/a4fe97449297af2e3be7841e1c18e431f3b06031/README.md)
- [Iris-3B model card](https://huggingface.co/speridlabs/iris-3b/blob/adc05b909fb05d4276d26aae5a52b36406b2ef26/README.md)
- [FLUX 3 licence](https://huggingface.co/black-forest-labs/flux-3-image-klein/blob/337337621b8526e120f2275d250242f4a6bedd75/LICENSE.md)

## Results by parameter variant

| Model            | Variant              | Size      | Steps |  OK | Failed | Skipped | Wall median (s) | Wall range (s) |
| ---------------- | -------------------- | --------- | ----: | --: | -----: | ------: | --------------: | -------------- |
| flux2-klein-4b   | 1024-4step           | 1024x1024 |     4 |  36 |      0 |       0 |           15.77 | 14.94-46.60    |
| flux2-klein-4b   | 1024-4step-alt-seed  | 1024x1024 |     4 |  36 |      0 |       0 |           15.79 | 15.03-18.77    |
| flux2-klein-4b   | 1024-8step           | 1024x1024 |     8 |  36 |      0 |       0 |           23.81 | 22.99-33.36    |
| flux2-klein-4b   | 512-4step            | 512x512   |     4 |  36 |      0 |       0 |            9.08 | 8.25-59.85     |
| flux2-klein-4b   | 768-4step            | 768x768   |     4 |  36 |      0 |       0 |           11.88 | 11.23-15.77    |
| flux3-klein      | 1024-alt-seed        | 1024x1024 |    16 |   0 |      0 |      36 |               - | -              |
| flux3-klein      | 1024-default         | 1024x1024 |    16 |   0 |      0 |      36 |               - | -              |
| flux3-klein      | 512-alt-seed         | 512x512   |    16 |  36 |      0 |       0 |           16.16 | 15.89-32.73    |
| flux3-klein      | 512-default          | 512x512   |    16 |  36 |      0 |       0 |           16.91 | 16.66-54.47    |
| flux3-klein      | 768-default          | 768x768   |    16 |  36 |      0 |       0 |           28.78 | 28.62-131.82   |
| iris-3b          | 1024-alt-seed        | 1024x1024 |   100 |  36 |      0 |       0 |          309.14 | 303.89-311.09  |
| iris-3b          | 1024-default         | 1024x1024 |   100 |  36 |      0 |       0 |          305.60 | 305.03-316.15  |
| iris-3b          | 512-alt-seed         | 512x512   |   100 |  36 |      0 |       0 |           90.30 | 87.63-98.27    |
| iris-3b          | 512-default          | 512x512   |   100 |  36 |      0 |       0 |           89.41 | 88.51-99.77    |
| iris-3b          | 768-default          | 768x768   |   100 |  36 |      0 |       0 |          188.21 | 174.05-204.31  |
| kromoss          | 1024-alt-seed        | 1024x1024 |    50 |   0 |      0 |      36 |               - | -              |
| kromoss          | 1024-default         | 1024x1024 |    50 |   1 |      0 |      35 |          341.18 | 341.18-341.18  |
| kromoss          | 512-alt-seed         | 512x512   |    50 |  36 |      0 |       0 |           73.77 | 73.16-88.91    |
| kromoss          | 512-default          | 512x512   |    50 |  36 |      0 |       0 |           74.01 | 73.32-134.58   |
| kromoss          | 768-default          | 768x768   |    50 |  36 |      0 |       0 |          171.57 | 170.05-180.94  |
| moss-image-sft   | 1024-alt-seed        | 1024x1024 |    50 |   0 |      0 |      36 |               - | -              |
| moss-image-sft   | 1024-default         | 1024x1024 |    50 |   0 |      0 |      36 |               - | -              |
| moss-image-sft   | 512-alt-seed         | 512x512   |    50 |  36 |      0 |       0 |           74.03 | 73.36-85.97    |
| moss-image-sft   | 512-default          | 512x512   |    50 |  36 |      0 |       0 |           74.24 | 73.43-148.06   |
| moss-image-sft   | 768-default          | 768x768   |    50 |  36 |      0 |       0 |          171.43 | 169.96-248.29  |
| sd35-medium      | 1024-40step          | 1024x1024 |    40 |  36 |      0 |       0 |           76.93 | 76.22-84.76    |
| sd35-medium      | 1024-alt-seed        | 1024x1024 |    20 |  36 |      0 |       0 |           41.69 | 40.85-71.54    |
| sd35-medium      | 1024-default         | 1024x1024 |    20 |  36 |      0 |       0 |           41.78 | 40.98-52.59    |
| sd35-medium      | 1024-guidance6       | 1024x1024 |    20 |  36 |      0 |       0 |           41.80 | 41.03-50.30    |
| sd35-medium      | 768-default          | 768x768   |    20 |  36 |      0 |       0 |           23.07 | 22.50-26.30    |
| sdxl-base        | 1024-alt-seed        | 1024x1024 |    20 |  36 |      0 |       0 |           15.87 | 15.72-18.19    |
| sdxl-base        | 1024-default         | 1024x1024 |    20 |  36 |      0 |       0 |           15.88 | 15.59-113.24   |
| sdxl-base        | 1024-fast            | 1024x1024 |    15 |  36 |      0 |       0 |           12.25 | 12.03-13.30    |
| sdxl-base        | 1024-high-guidance   | 1024x1024 |    20 |  36 |      0 |       0 |           15.87 | 15.69-17.61    |
| sdxl-base        | 768-default          | 768x768   |    20 |  36 |      0 |       0 |            8.53 | 8.43-99.13     |
| sdxl-fast        | 1024-4step           | 1024x1024 |     4 |  36 |      0 |       0 |            2.83 | 2.69-3.47      |
| sdxl-fast        | 1024-6step           | 1024x1024 |     6 |  36 |      0 |       0 |            3.57 | 3.47-5.31      |
| sdxl-fast        | 1024-8step           | 1024x1024 |     8 |  36 |      0 |       0 |            4.31 | 4.21-4.98      |
| sdxl-fast        | 1024-8step-alt-seed  | 1024x1024 |     8 |  36 |      0 |       0 |            4.30 | 4.17-4.81      |
| sdxl-fast        | 1024-8step-guidance1 | 1024x1024 |     8 |  36 |      0 |       0 |            4.33 | 4.20-4.66      |
| sdxl-hypersd     | 1024-4step           | 1024x1024 |     4 |  36 |      0 |       0 |            2.80 | 2.70-3.10      |
| sdxl-hypersd     | 1024-6step           | 1024x1024 |     6 |  36 |      0 |       0 |            3.53 | 3.45-3.74      |
| sdxl-hypersd     | 1024-8step           | 1024x1024 |     8 |  36 |      0 |       0 |            4.26 | 4.15-4.43      |
| sdxl-hypersd     | 1024-8step-alt-seed  | 1024x1024 |     8 |  36 |      0 |       0 |            4.26 | 4.18-5.06      |
| sdxl-hypersd     | 1024-8step-guidance1 | 1024x1024 |     8 |  36 |      0 |       0 |            4.23 | 4.18-4.55      |
| ssd-1b           | 1024-20step          | 1024x1024 |    20 |  36 |      0 |       0 |           10.06 | 9.97-10.42     |
| ssd-1b           | 1024-25step          | 1024x1024 |    25 |  36 |      0 |       0 |           12.23 | 11.99-12.42    |
| ssd-1b           | 1024-30step          | 1024x1024 |    30 |  36 |      0 |       0 |           14.41 | 14.26-14.58    |
| ssd-1b           | 1024-alt-seed        | 1024x1024 |    20 |  36 |      0 |       0 |           10.02 | 9.93-10.26     |
| ssd-1b           | 768-20step           | 768x768   |    20 |  36 |      0 |       0 |            5.44 | 5.35-5.62      |
| ssd-1b-lightning | 1024-4step           | 1024x1024 |     4 |  36 |      0 |       0 |            2.25 | 2.12-2.34      |
| ssd-1b-lightning | 1024-6step           | 1024x1024 |     6 |  36 |      0 |       0 |            2.75 | 2.64-2.93      |
| ssd-1b-lightning | 1024-8step           | 1024x1024 |     8 |  36 |      0 |       0 |            3.16 | 3.08-3.36      |
| ssd-1b-lightning | 1024-8step-alt-seed  | 1024x1024 |     8 |  36 |      0 |       0 |            3.17 | 3.06-3.34      |
| ssd-1b-lightning | 768-8step            | 768x768   |     8 |  36 |      0 |       0 |            1.86 | 1.73-2.00      |
| z-image-turbo    | 1024-4step           | 1024x1024 |     4 |   0 |      0 |      36 |               - | -              |
| z-image-turbo    | 1024-8step           | 1024x1024 |     8 |   0 |      0 |      36 |               - | -              |
| z-image-turbo    | 1024-8step-alt-seed  | 1024x1024 |     8 |   0 |      0 |      36 |               - | -              |
| z-image-turbo    | 512-8step            | 512x512   |     8 |  36 |      0 |       0 |           13.34 | 12.80-28.15    |
| z-image-turbo    | 768-8step            | 768x768   |     8 |  36 |      0 |       0 |           20.02 | 19.50-54.45    |
