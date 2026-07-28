# GPU performance reference

Where generation time actually goes on the reference card, what was tried to
reduce it, and how the numbers translate to other hardware. Every measured
figure here comes from the worker's own engine on the reference box, not from
vendor claims or literature.

Companion documents: [third-party-models.md](third-party-models.md) for
licensing, [scripts/BENCHMARK.md](../scripts/BENCHMARK.md) for the harness,
[decisions.md](decisions.md) for the decisions these measurements informed.

## The reference card

| Property | Value |
| --- | --- |
| Model | AMD Radeon RX 7600 XT |
| Architecture | RDNA 3, Navi 33, `gfx1102` |
| Compute units | 32 CUs (16 WGPs), 2048 stream processors |
| Boost clock | 2755 MHz rated, 2520 MHz observed under sustained load |
| FP32 | ~11.3 TFLOPS peak |
| FP16 | ~22.6 TFLOPS peak (RDNA 3 dual-issue) |
| VRAM | 16 GB GDDR6, 128-bit bus, 18 Gbps, ~288 GB/s |
| Host link | PCIe 4.0 x8 |
| Board power | 165 W |
| Stack | torch 2.9.1+rocm6.3, diffusers 0.39.0 |

Two caveats worth knowing before reading any number below.

`torch.cuda.get_device_properties().multi_processor_count` reports **16** on
this card. That is WGPs, not CUs. The card has 32 CUs. Do not use the torch
figure for throughput math.

Usable VRAM is **not** 16 GB. The card reports 15.98 GiB total and the desktop
session holds 1.2 to 2.0 GiB of it, so roughly **14.3 GiB** is available to a
worker. Several conclusions below turn on that difference.

RDNA 3 has WMMA instructions but nothing equivalent to the dedicated tensor
cores in NVIDIA's datacenter parts. The FP16 figure above is shader
throughput, which is why it sits an order of magnitude below an A100's
quoted tensor number and why those two numbers must never be compared
directly.

## The roster at its manifest ceiling

Every text-to-image model at the highest steps and largest resolution its
manifest allows, across the 60-prompt suite in
`scripts/benchmark-prompts-60.json`. 600 images, 600 successes, no failures.
Measured 2026-07-27 on the reference card through the API and worker, not
through the engine directly, so these include the real dispatch path.

| Model | Res | Steps | Median | Min | Max | Load |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| sd-turbo | 512 | 4 | 0.43 s | 0.43 s | 0.64 s | 5.0 s |
| sdxl-turbo | 512 | 4 | 0.64 s | 0.64 s | 0.85 s | 15.1 s |
| vega-rt | 1024 | 8 | 1.96 s | 1.91 s | 2.37 s | 6.3 s |
| ssd-1b-lightning | 1024 | 8 | 2.61 s | 2.58 s | 2.90 s | 8.3 s |
| sdxl-hypersd | 1024 | 8 | 3.77 s | 3.73 s | 4.08 s | 11.1 s |
| sdxl-fast | 1024 | 8 | 3.77 s | 3.74 s | 4.08 s | 10.4 s |
| dreamshaper-lcm | 768 | 15 | 5.57 s | 5.56 s | 6.05 s | 4.6 s |
| ssd-1b | 1024 | 40 | 18.55 s | 18.20 s | 18.61 s | 9.8 s |
| sdxl-base | 1024 | 50 | 37.34 s | 37.23 s | 37.68 s | 15.1 s |
| **sd35-medium** | 1024 | 50 | **110.94 s** | 110.52 s | 119.08 s | 2.2 s |

This is a ceiling comparison, not a defaults comparison. Several of these
models ship far lower defaults because their distillation does not benefit
from more steps: `vega-rt` runs 2 steps at 512 in the studio, not 8 at 1024.
For the numbers the studio picker actually shows, see
`backend/app/model_timings.json`.

Read across the table and the spread is 258x, from `sd-turbo` at 0.43 s to
`sd35-medium` at 110.94 s. The distilled 8-step models cluster tightly around
2 to 4 seconds and are the practical working tier. `sdxl-fast` and
`sdxl-hypersd` are within 0.01 s of each other, which is worth remembering
given `sdxl-hypersd` remains benchmark-only over its undeclared LoRA license:
there is nothing to gain by promoting it.

`sd35-medium` is 3x its own 20-step default for a quality difference that did
not survive inspection at a fixed seed, which is why the manifest defaults to
20. Its load time of 2.2 s looks anomalous next to `sdxl-base` at 15.1 s only
because the offload rung leaves the weights on the host: it never pays a full
transfer to VRAM at load, it pays it during every generation instead.

## Measured: where the time goes

All figures at 1024x1024, 20 steps, fp16, warm (second run discarded the
first), `sd35-medium`, prompt fixed and seed fixed.

| Configuration | Time | Peak VRAM | Note |
| --- | ---: | ---: | --- |
| `model_offload`, T5 resident (previous) | 49.5 s | 12.09 GB | The previous 16 GB configuration |
| `group_offload` | 46.5 s | 2.51 GB | Faster **and** 4.8x lighter |
| 768 px instead of 1024 | 27.8 s | 12.09 GB | 1.78x fewer pixels, 1.78x less time |
| Flash attention backend | 49.9 s | 12.09 GB | No effect; fused attention was already on |
| No T5, `full` residency | 40.9 s | 8.83 GB | Loses the long-prompt window |
| No T5, `full` + `torch.compile` | **26.0 s** | 8.84 GB | Fastest measured, but no T5 |
| `full` residency with T5 | **OOM** | - | 15.15 GB of weights, 14.3 GiB available |

Step scaling on the previous configuration is roughly a fixed floor plus a
per-step cost: 56 s at 20 steps, 65 s at 28, 89 s at 40 on first-run timings,
which works out near 1.7 s per step over a fixed overhead in the low twenties
of seconds. The per-step cost is transformer compute and the floor is text
encoding, component transfers and VAE decode.

Guidance above 1.0 enables classifier-free guidance, which runs the
transformer on a doubled batch and therefore roughly doubles per-step cost.
`sd35-medium` defaults to guidance 4.5 because it needs CFG for quality, so
that doubling is paid on every step. This is not a defect to optimize away;
it is the cost of the quality the model is selected for.

## The cost model

Generation time on this card decomposes cleanly. For `sdxl-base` at 1024 px,
measured by running the same pipeline at 10 and 20 steps and solving for the
constant, then repeating with `output_type="latent"` to remove the decode:

```
total = 717 ms fixed  +  steps x 750 ms
        |                        |
        |                        +-- 2 UNet evaluations at ~375 ms (CFG doubles it)
        +-- 667 ms VAE decode + 50 ms text encode and setup
```

Both halves were verified against the measurements they predict: 10 steps
gives 8218 ms and 20 gives 15719 ms, and the fit reproduces them exactly. Step
scaling is perfectly linear from 10 to 50 steps.

Three consequences worth internalising.

**The fixed cost is almost entirely VAE decode.** 667 ms of the 717 ms floor
is turning the final latent into pixels, and it is paid once per image no
matter how many steps ran. Text encoding is 50 ms, which is noise. For a
50-step render the floor is 2% of runtime and irrelevant; for `vega-rt` at
1.87 s it is **36% of the total**, and on the fast tier it is the single
largest optimisation target left. `AutoencoderTiny` (TAESD) decodes in
roughly 10 ms instead of 667 ms at some fidelity cost, and the deferred
realtime ladder in [decisions.md](decisions.md) already anticipates exactly
this ("tiny-autoencoder decode for the live preview with full VAE on refine").
This measurement is the quantitative case for it.

**Guidance above 1.0 doubles per-step cost.** Diffusers sets
`do_classifier_free_guidance = guidance_scale > 1.0`, so every step runs the
UNet on a batch of two, conditional and unconditional. That is why the roster
spans 258x: `sdxl-fast` at 8 steps with guidance 0 runs 8 UNet evaluations,
`sdxl-base` at 50 steps with guidance 6 runs 100, on the same 3.5B UNet.
Distilled models set guidance 0 and cap it at 2 in the manifest precisely
because their distillation bakes the guidance effect into the weights;
applying CFG on top oversaturates.

**Steps are linear and buy less than they cost.** See the sweep below.

## Step count: 50 buys nothing over 20

`sdxl-base` and `ssd-1b` already ship `dpmsolver` (DPM++ 2M Karras), a
fast-converging solver. The question is therefore not which scheduler but how
few steps it needs. Same prompt, same seed 100, 1024 px, guidance 6:

| Steps | dpmsolver | euler-trailing | stock Euler |
| ---: | ---: | ---: | ---: |
| 10 | 8.29 s | - | - |
| 15 | 12.02 s | - | - |
| 20 | 15.76 s | 15.72 s | 15.70 s |
| 25 | 19.51 s | - | - |
| 30 | 23.27 s | 23.17 s | 23.15 s |
| 40 | 30.66 s | - | - |
| 50 | 38.03 s | - | - |

Scheduler choice does not change runtime at matched steps, to within 60 ms.
Solvers change the trajectory through latent space, not the arithmetic per
step, so any speed argument between them is really an argument about how few
steps each needs to converge.

On quality, 20 and 50 steps are effectively indistinguishable at a fixed seed:
same composition, same anatomy, marginally cleaner background detail at 50 for
2.4x the time. Even 10 steps produces a sharp, well-formed image. Note that
composition shifts with step count because the trajectory differs, so this is
not a strict quality ladder, but there is no degradation to point at.

The practical conclusion: the shipped default of 20 is sound and the 50-step
manifest ceiling exists for headroom rather than because anyone should use it.
The roster benchmark ran ceilings deliberately, which is why `sdxl-base` shows
37.34 s there against 15.76 s at its default.

## Batching does not work on this card

The obvious way to spend spare VRAM is to generate several images per denoise
loop. Measured with `num_images_per_prompt`, per-image time and peak VRAM:

| Model | batch 1 | batch 2 | batch 4 | batch 8 |
| --- | ---: | ---: | ---: | ---: |
| sdxl-fast | 3.69 s / 9.44 GB | 3.67 s / 14.22 GB | OOM | - |
| ssd-1b-lightning | 2.55 s / 6.91 GB | 2.50 s / 9.38 GB | 3.15 s / 14.44 GB | OOM |
| vega-rt | 1.87 s / 5.71 GB | 1.83 s / 8.18 GB | 1.82 s / 13.12 GB | OOM |
| sdxl-base | 15.34 s / 9.07 GB | 15.12 s / 11.54 GB | OOM | - |

Best case is a 3% improvement. `ssd-1b-lightning` at batch 4 is 19% **worse**
than batch 1. Everything OOMs by batch 4 or 8, because VRAM grows steeply
(9.44 to 14.22 GB going from one image to two on `sdxl-fast`).

### Why batching works everywhere else but not here

The flat result above is easy to misread as "GPUs cannot process images
concurrently". They can, it is how every inference provider operates, and the
result here is a property of *this* card rather than of GPUs.

A GPU runs one kernel across thousands of threads grouped into workgroups,
scheduled onto compute units. Throughput depends on **occupancy**: whether
there is enough independent work in flight to keep every CU busy and to hide
memory latency behind arithmetic. Batching adds independent work along the
batch dimension, so it raises occupancy whenever occupancy is the thing you
are short of.

That is the whole story. Batching helps exactly when the GPU is starved, and
this card is not starved:

- A 1024 px SDXL latent is 128x128. Every convolution in the UNet already
  unfolds into millions of independent output elements, which is far more
  parallel work than 32 CUs can consume at once. The machine is saturated by
  one image, so a second image simply queues behind the first. Time doubles,
  per-image time does not improve, and that is precisely what the table shows.
- An A100 has 108 SMs and roughly 14x the FP16 throughput. There, one 1024 px
  image genuinely leaves the machine partly idle, and batch 4 or 8 costs far
  less than 4x or 8x the time of batch 1. This is why datacenter inference is
  quoted in images per second per GPU rather than seconds per image.

Two further mechanisms matter at enterprise scale and neither applies to a
single consumer card:

**Continuous batching across requests.** Providers do not batch one user's
four images; they batch four different users' requests into one denoise loop,
refilling slots as requests complete. That converts idle occupancy into
throughput at high request volume. It needs many concurrent requests to be
worth it, which a self-hosted install does not have.

**Multi-GPU and partitioning.** Large deployments run many GPUs, and a single
A100 or H100 can be partitioned (MIG) into instances serving separate streams.
Concurrency there comes from having more silicon, not from making one image
faster.

So the honest statement is not "batching does not work" but **"batching
converts spare occupancy into throughput, and this card has no spare
occupancy"**. The same experiment on a rented A100 would very likely show
large gains, and the cloud fleet should measure it rather than inherit this
conclusion. What it does mean concretely: implementing queued-job
micro-batching for a 16 GB self-hosted box would deliver nothing, which is
consistent with that work already being deferred in
[decisions.md](decisions.md).

One caveat on the numbers above: batching also does not help **latency** even
on a large GPU. Every image in a batch finishes when the slowest does, so
per-image latency never improves; only images-per-second does. For an
interactive studio, latency is what a user feels.

## What was tried, and what it bought

**`group_offload` instead of `model_offload`.** Worth knowing about. The
memory ladder assumes rungs trade speed for VRAM monotonically, so
`group_offload` sits at the bottom as the slow-but-survivable rung. For SD3
that assumption does not hold: leaf-level streaming with prefetch overlaps
transfer with compute, and a 9.12 GB text encoder that runs once per image
then sits idle is exactly the shape that suits. It came out marginally faster
while using a fifth of the memory. The ladder still selects `model_offload`
automatically, and that is left alone deliberately, because one model
contradicting the ordering does not justify rewriting rung selection for all
of them. Recorded here so the next person does not assume the bottom rung is
always the slow one.

**`torch.compile`.** The largest single win measured anywhere: 40.9 s down to
26.0 s, a 36% reduction. It was unreachable in the previous configuration.
Compile is applied only to full-resident pipelines because accelerate's
offload hooks and Inductor fight each other, so a model forced onto an offload
rung cannot have it. On this card T5-XXL forces the offload rung, so the 36%
stays locked away.

**Dropping T5-XXL.** Frees 9.12 GB, brings full residency within reach, and
unlocks compile. It also discards the entire reason for choosing SD 3.5 over
SDXL: the >77-token prompt window that answers issues #147 and #148. Measured
for diagnosis, not proposed as a product configuration.

**Lower resolution.** 768 px costs 27.8 s against 49.5 s, tracking the pixel
ratio almost exactly. This is the one lever a user can pull today, and the
manifest exposes 768 alongside 1024 for that reason.

**Flash attention backend.** No measurable change, and the reason is that
there was nothing to gain. `DiffusersEngine.__init__` already sets
`TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` for `DEVICE=rocm`, which is what
gates the fused attention kernels on RDNA 3; without it torch falls back to
math attention, which is several times slower. The baseline therefore already
runs fused attention through torch SDPA, and an explicit backend can only
match it or regress.

Any standalone ROCm test script must set
`TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` before SDPA first dispatch. Without
it SDPA falls back to math attention and allocates a 3.51 GiB attention matrix.

Of the backends diffusers 0.39 exposes, none improves on that here. The
FlashAttention-3 entries target Hopper. `aiter` is AMD's own kernel library
and is the one plausible candidate, but it is not installed, as are
`flash_attn`, `xformers` and `sageattention`. Adding attention backends to
this card is a dependency decision with no demonstrated headroom behind it.

**Quantizing T5.** The one that paid off, covered in full above. Two
practical notes for anyone repeating it.

`bitsandbytes` does not work on this card. It installs, warns that it is
substituting a ROCm 6.4 binary for ROCm 6.3, and then fails on the GPU with
`Error invalid device function at line 432 in file /src/csrc/ops.cu`. Its
int8 kernels are not built for gfx1102. Do not spend time on it.

`torchao` does work, with `Int8WeightOnlyConfig`, despite warning at import
that its C++ extensions want torch >= 2.11 (this environment has 2.9.1). A
quantised linear layer matches its fp16 counterpart to five decimal places.

The upstream `t5xxl_fp8_e4m3fn.safetensors` at 4.56 GB is a red herring:
RDNA 3 has no native fp8 matmul, so torch upcasts it on load and the saving
evaporates. int8 weight-only is the format that works here.

## The headline conclusion: int8 T5 breaks the deadlock

An earlier revision of this document concluded that a 16 GB card cannot have
all three of T5-XXL, full residency and `torch.compile`, because 15.15 GB of
fp16 weights do not fit in ~14.3 GiB. That conclusion was correct about fp16
and wrong as a general statement. Quantising T5 to int8 with `torchao` fits
everything, and the payoff is large:

| Configuration | Time | Peak VRAM | T5? |
| --- | ---: | ---: | --- |
| fp16 T5, `model_offload` (previous) | 49.5 s | 12.09 GB | Yes |
| int8 T5, full residency, eager | 43.1 s | 13.44 GB | Yes |
| **int8 T5, full residency, `torch.compile`** | **28.0 s** | 13.44 GB | **Yes** |
| fp16, no T5, full residency + compile | 26.0 s | 8.84 GB | No |

**1.77x faster than the previous configuration, with the long-prompt window
intact.** int8 brings T5 from 9.12 GB to 4.57 GB, which puts the whole
pipeline at 10.93 GB resident against 15.15 GB before. Full residency then
makes `torch.compile` available, and compile is worth 35% here (43.1 s to
28.0 s), consistent with the 36% measured on the no-T5 pipeline.

It lands within 2 s of the no-T5 configuration while keeping the feature that
no-T5 throws away.

Quality survives. On a 90-word prompt the int8 pipeline still renders detail
from past CLIP's 77-token cutoff, including a cat and a lamp named only in the
tail. Weight-only int8 perturbs the text encoder slightly; it did not cost
prompt comprehension, which is the property that matters for choosing SD 3.5
over SDXL in the first place.

Two implementation constraints follow from the measurements:

- 13.44 GB peak against ~14.3 GiB usable is **tight**. A desktop session
  holding more VRAM than usual can push it back to OOM. Under
  `MEMORY_MODE=auto`, the worker now descends one rung after its generation
  eviction retry fails, reloads and retries the job once.
- It adds a `torchao` dependency to the worker and makes `torch.compile`
  required for the quantized full-resident pipeline, which is a change to the
  engine's failure surface, not just its speed.

Issue #155 adopts this path for `sd35-medium` only. CUDA workers still need
their own acceptance measurement.

## int8 does not generalize to the other models

int8 helps `sd35-medium` because that model does not fit. The other models
already fit. This section records what int8 does to them, so nobody repeats
the test.

Measured at 1024 px, `sdxl-base` at 20 steps and guidance 6, `sdxl-fast` at
8 steps and guidance 0. Times are per image.

| Model | Weights | batch 1 | batch 2 | batch 4 |
| --- | --- | ---: | ---: | ---: |
| sdxl-base | fp16 | 15.66 s / 9.07 GB | 15.23 s / 11.54 GB | OOM |
| sdxl-base | int8 | 15.84 s / 6.98 GB | 15.10 s / 9.45 GB | OOM |
| sdxl-fast | fp16 | 3.87 s / 11.62 GB | OOM | OOM |
| sdxl-fast | int8 | 4.01 s / 7.22 GB | 3.77 s / 9.69 GB | OOM |

Three results follow from the table.

int8 costs a small amount of speed at batch 1. `sdxl-base` slows by 1.1 percent
and `sdxl-fast` by 3.6 percent. `Int8WeightOnlyConfig` stores the weights as
int8 and converts them back to fp16 for each matrix multiply. RDNA 3 has no
fast int8 matmul, so the conversion adds work and returns nothing.

int8 frees real memory. `sdxl-base` drops 2.09 GB and `sdxl-fast` drops
4.40 GB. This is the same mechanism that rescues `sd35-medium`.

The freed memory buys almost no speed. `sdxl-fast` at batch 2 becomes possible
under int8, where fp16 runs out of memory. It then delivers 3.77 s per image
against 3.87 s for fp16 at batch 1, which is 2.6 percent. The card stays
compute-bound, so a larger batch still finds no idle capacity to use.

The conclusion is narrow and worth stating plainly. int8 solves a memory
problem. Only `sd35-medium` has a memory problem. For every other model in the
roster, int8 trades a little speed for memory that the model does not need.

## Which limits are which

Every slow thing measured above falls into one of three categories, and the
category determines whether spending money, changing code, or neither will
help.

| Limit | Category | Escapable? |
| --- | --- | --- |
| CFG doubles per-step compute | Fundamental | No, not while you want CFG |
| Steps multiply evaluations linearly | Fundamental | No |
| Denoising cannot be parallelised across steps | Fundamental | No |
| SD 3.5 cannot be fully resident **in fp16** | This GPU | **Yes, int8 T5 fixes it today** |
| `torch.compile` unavailable for SD 3.5 | This GPU | **Yes, follows from int8 residency** |
| Batching gains nothing | This GPU | Yes, a larger GPU |
| ~375 ms per SDXL UNet evaluation | This GPU | Yes, faster silicon |
| No fp8 quantisation path | This GPU | No, RDNA 3 lacks fp8 matmul |
| int8 quantisation | Stack only | **Solved, torchao works** |
| T5-XXL costs 9.12 GB | The model | No, inherent to SD 3.5 |
| Distilled models refuse CFG | The model | No, by design |
| 667 ms VAE decode per image | The model | **Yes, and it is the best target left** |
| More steps stop helping around 20 | The model | No, that is convergence |

### Fundamental: no hardware or software bypasses these

**Diffusion is sequential.** Step N needs step N-1's latent. You cannot spend
parallelism, VRAM or money to compute steps concurrently. This is the root
reason the batching result came out flat and why latency has a hard floor
independent of how large a GPU you buy.

**Classifier-free guidance costs exactly 2x.** Any `guidance_scale > 1.0`
evaluates the network twice per step. The only escape is not using CFG, which
is what distillation does, and that is a different model rather than a faster
one.

**Steps are linear.** Measured across 10 to 50 steps with no sublinearity to
exploit. Fewer steps is the only lever, which makes solver convergence and
distillation the real levers.

### This GPU: a bigger card fixes these

**16 GB is the binding constraint, more than compute is.** SD 3.5 Medium needs
15.15 GB of fp16 weights against ~14.3 GiB usable after the desktop. That
single fact forces the offload rung, and the offload rung forfeits
`torch.compile`. A 24 GB card would give back both at once, and so, as it
turns out, does int8 quantisation on this card: 10.93 GB resident, 28.0 s
against 49.5 s. The constraint is real but it is a memory constraint, which
means it has software answers as well as hardware ones. The projection table
below makes the same point from the other side: a V100 32 GB is expected to
roughly double a V100 16 GB on identical compute, purely because the pipeline
fits.

**Batching is dead here specifically because the card is small.** 32 CUs are
saturated by one 1024 px image. The same experiment on an A100 would very
likely show real gains, because there a single image leaves the machine idle.
Do not generalise the flat result above to other hardware.

**Raw throughput is what it is.** ~375 ms per SDXL UNet evaluation at 1024 px
on ~11.3 TFLOPS fp32 / ~22.6 TFLOPS fp16 shader throughput, with no
datacenter-class tensor cores. Faster silicon is the only answer.

**Quantisation is limited by both hardware and stack.** `torchao`
`Int8WeightOnlyConfig` works on the reference card through its pure PyTorch
path. `bitsandbytes` does not: its int8 kernel is not built for gfx1102. RDNA 3
still has no native fp8 matmul, so the upstream `t5xxl_fp8` file is upcast on
load and saves nothing.

### The model: inherent to the weights, except one

**T5-XXL is 9.12 GB and that is the point of it.** It is what gives SD 3.5 a
prompt window past CLIP's 77 tokens, which is the entire reason the model was
chosen over SDXL. Removing it measured 26.0 s against 49.5 s, and would
discard the feature.

**Distilled models cannot take CFG.** Lightning, Hyper-SD, Turbo and LCM bake
guidance into the weights. Their manifests cap guidance at 2 as a guard rail.
This is not a limitation to fix; it is the trade that makes them 8-step models.

**Convergence stops around 20 steps.** DPM++ has essentially converged by
then, so the remaining 30 steps of the manifest ceiling buy nothing.

**VAE decode is the exception, and it is the best remaining target.** 667 ms
per image regardless of resolution-independent work, model, or step count. On
a 50-step render it is 2% and invisible; on `vega-rt` at 1.87 s it is 36% of
total runtime. `AutoencoderTiny` decodes in roughly 10 ms. Nothing about the
hardware forces this cost, and the deferred realtime ladder already names the
approach. Of everything measured in this document, this is the one large win
that is neither blocked by VRAM nor by physics.

## Projecting to other hardware

Two independent factors decide the time on another card, and the VRAM one
usually dominates:

1. **Does the pipeline fit fully resident?** Fitting removes the offload
   overhead and unlocks `torch.compile`, worth 36% on its own. SD 3.5 Medium
   in fp16 needs roughly 15.2 GB of weights plus a few GB of activations, so
   it wants **20 GB or more** to sit resident comfortably. Anything at 16 GB
   or below is forced onto an offload rung regardless of how fast it computes.
2. **Compute throughput**, which sets the per-step cost.

There is no "Tesla T100"; NVIDIA's datacenter line runs T4, V100, A100, H100,
so the table uses real parts.

| GPU | VRAM | FP16 throughput | Fits resident? | Estimated 1024/20 |
| --- | ---: | --- | --- | ---: |
| RX 7600 XT (this card) | 16 GB | ~22.6 TFLOPS shader | Yes, with int8 T5 | **28.0 s measured** |
| Tesla T4 | 16 GB | ~65 TFLOPS tensor | No, offload forced | 40 to 70 s |
| Tesla V100 16 GB | 16 GB | ~125 TFLOPS tensor | No, offload forced | 25 to 40 s |
| Tesla V100 32 GB | 32 GB | ~125 TFLOPS tensor | Yes | 10 to 18 s |
| RTX 4090 | 24 GB | ~165 TFLOPS tensor (dense) | Yes | 6 to 12 s |
| A100 40/80 GB | 40/80 GB | ~312 TFLOPS tensor | Yes | 4 to 8 s |
| H100 80 GB | 80 GB | ~756 TFLOPS tensor (dense) | Yes | 2 to 5 s |

**Every figure in the last column except the first is an estimate, not a
measurement**, and the ranges are deliberately wide. The estimates for the
other cards remain fp16 projections because the int8 path has not been
accepted on CUDA. Diffusion rarely scales
with peak FLOPS: real speedups land well below the paper ratio because
attention is partly memory-bound, and the fixed costs of text encoding and VAE
decode do not shrink with tensor throughput. Treat the ordering as reliable
and the absolute numbers as rough.

The interesting row is the two V100 variants. Identical compute, and the
32 GB part is expected to be roughly twice as fast purely because the pipeline
fits. On a card this size, capacity beats throughput.

For self-hosters on 16 GB or less, `group_offload` at 2.51 GB peak means
`sd35-medium` will run on far smaller cards than its `min_vram_gb` suggests;
it will simply be slow.

## Why `min_vram_gb` is 14

The int8 pipeline is 10.93 GB resident and reaches 13.44 GB during generation.
`min_vram_gb` is the full-residency requirement, so 14 rounds up from the
measured peak rather than from the weight-only resident figure. This lets a
reference-card worker with about 14.3 GiB free select full residency while a
busier desktop can select model offload before attempting a tight load.

Free memory can change between rung selection and the generation peak. The
automatic fallback therefore remains required even with the measured value:
after the existing eviction retry fails, generation descends one rung, reloads
and retries once.

## Reproducing

The stack must be up: `make deps`, then `make api` (with `BENCHMARK_API=1`)
and `make worker-rocm`.

```bash
# Every model at its manifest ceiling across the 60-prompt suite
backend/.venv/bin/python scripts/benchmark.py \
  --prompts scripts/benchmark-prompts-60.json \
  --matrix scripts/benchmark-matrix-best.json \
  --include-capped --continue-on-error
```

Stop competing GPU workloads first. An earlier issue #75 run was invalidated
by an unrelated process holding ~14 GB, and on a card this size anything
resident changes which rung the ladder picks.

## The int8 path

Issue #155 adds one worker-only manifest field naming a component and scheme.
`sd35-medium` declares `text_encoder_3:int8`; no other model is quantized.
The engine applies torchao immediately after `_from_pretrained`, before the
device move, and requires compile when that quantized pipeline is fully
resident.

Load-time out-of-memory errors under `MEMORY_MODE=auto` descend from full
residency to model offload, then to group offload. Generation first evicts
other residents and retries as before. If that still fails, it descends one
rung, reloads and retries the job once. Explicitly pinned memory modes never
descend.

## The 15-step default

The 50-step ceiling buys nothing over 20. A wider sweep shows that 20 also
buys little over 15. Six prompts, four step counts, two models, seed 100,
1024 px, dpmsolver.

| Model | 12 steps | 15 steps | 20 steps | 30 steps |
| --- | ---: | ---: | ---: | ---: |
| sdxl-base | 9.73 s | 11.96 s | 15.69 s | 23.09 s |
| ssd-1b | 6.21 s | 7.58 s | 9.81 s | 14.27 s |

Both models moved from a 20-step default to 15. That returns 24 percent of the
time on every image the studio makes with them.

The visual check covered a portrait with hands and a face, a botanical
illustration with fine linework, and a forge scene with sparks. Faces, hands
and crosshatching stay clean at 15 steps. They also stay clean at 12, which
suggests more headroom, but the sample does not support a further cut yet.

Read this as a sample, not a proof. The sweep produced 48 images and a person
looked at four of them. Step count changes the path through latent space, so
two step counts give different compositions rather than the same image at two
levels of polish. That rules out a pixel metric and leaves human judgment.

## Tuned settings against manifest ceilings

The cosmic suite (`scripts/benchmark-prompts-cosmic.json`, 60 prompts on black
holes, deep space and cosmic fantasy) ran the whole roster twice over: once at
manifest ceilings, once at the settings this project now believes are correct.
600 images each time, no failures either time.

| Model | Ceiling | Tuned | Change |
| --- | ---: | ---: | ---: |
| sdxl-base | 37.34 s at 50 steps | **11.69 s at 15** | 3.2x faster |
| ssd-1b | 18.55 s at 40 steps | **7.37 s at 15** | 2.5x faster |
| sd35-medium | 110.94 s at 50 steps | **48.33 s at 20** | 2.3x faster |
| sdxl-fast | 3.77 s at 8 steps | 3.77 s at 8 | unchanged |
| sdxl-hypersd | 3.77 s at 8 steps | 3.77 s at 8 | unchanged |
| ssd-1b-lightning | 2.61 s at 8 steps | 2.62 s at 8 | unchanged |
| vega-rt | 1.96 s at 8 steps | 1.96 s at 8 | unchanged |
| dreamshaper-lcm | 5.57 s at 15 steps | 5.58 s at 15 | unchanged |
| sdxl-turbo | 0.64 s at 4 steps | 0.64 s at 4 | unchanged |
| sd-turbo | 0.43 s at 4 steps | 0.43 s at 4 | unchanged |

The distilled models do not move, because their design point already is their
ceiling. Only the three models that take real step counts had anything to
give back, and they gave back a lot.

The whole suite fell from 3 hours 7 minutes to 1 hour 31 minutes. Nothing in
the roster got worse.

Read the two runs together and the point is simple. The ceilings were never a
recommendation. They were headroom that cost 2.3 to 3.2 times the time and
returned quality that did not survive inspection at a fixed seed.
