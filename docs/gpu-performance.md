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

## Measured: where the time goes

All figures at 1024x1024, 20 steps, fp16, warm (second run discarded the
first), `sd35-medium`, prompt fixed and seed fixed.

| Configuration | Time | Peak VRAM | Note |
| --- | ---: | ---: | --- |
| `model_offload`, T5 resident (shipped) | 49.5 s | 12.09 GB | The shipped 16 GB configuration |
| `group_offload` | 46.5 s | 2.51 GB | Faster **and** 4.8x lighter |
| 768 px instead of 1024 | 27.8 s | 12.09 GB | 1.78x fewer pixels, 1.78x less time |
| Flash attention backend | 49.9 s | 12.09 GB | No effect; fused attention was already on |
| No T5, `full` residency | 40.9 s | 8.83 GB | Loses the long-prompt window |
| No T5, `full` + `torch.compile` | **26.0 s** | 8.84 GB | Fastest measured, but no T5 |
| `full` residency with T5 | **OOM** | - | 15.15 GB of weights, 14.3 GiB available |

Step scaling on the shipped configuration is roughly a fixed floor plus a
per-step cost: 56 s at 20 steps, 65 s at 28, 89 s at 40 on first-run timings,
which works out near 1.7 s per step over a fixed overhead in the low twenties
of seconds. The per-step cost is transformer compute and the floor is text
encoding, component transfers and VAE decode.

Guidance above 1.0 enables classifier-free guidance, which runs the
transformer on a doubled batch and therefore roughly doubles per-step cost.
`sd35-medium` defaults to guidance 4.5 because it needs CFG for quality, so
that doubling is paid on every step. This is not a defect to optimize away;
it is the cost of the quality the model is selected for.

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
26.0 s, a 36% reduction. It is also unreachable in the shipped configuration.
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

Of the backends diffusers 0.39 exposes, none improves on that here. The
FlashAttention-3 entries target Hopper. `aiter` is AMD's own kernel library
and is the one plausible candidate, but it is not installed, as are
`flash_attn`, `xformers` and `sageattention`. Adding attention backends to
this card is a dependency decision with no demonstrated headroom behind it.

**Quantizing T5.** Not attempted, because it cannot be attempted here.
`bitsandbytes`, `torchao` and `optimum.quanto` are all absent from the worker
environment. The upstream repository does ship `t5xxl_fp8_e4m3fn.safetensors`
at 4.56 GB against 9.12 GB for fp16, but RDNA 3 has no native fp8 matmul, so
torch would upcast on load and the saving would evaporate. This is the single
most promising unexplored avenue and it needs a dependency decision first.

## The headline conclusion

On a 16 GB card, SD 3.5 Medium cannot have all three of T5-XXL, full
residency, and `torch.compile`. The weights alone are 15.15 GB against 14.3
GiB of usable VRAM. Ship T5 and you take the offload rung, which forfeits
compile. Drop T5 and you get 26.0 s but lose the reason the model was chosen.

The shipped configuration takes the first branch deliberately. A working
int8 T5 would break the deadlock and is the obvious next investigation.

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
| RX 7600 XT (this card) | 16 GB | ~22.6 TFLOPS shader | No, offload forced | **49.5 s measured** |
| Tesla T4 | 16 GB | ~65 TFLOPS tensor | No, offload forced | 40 to 70 s |
| Tesla V100 16 GB | 16 GB | ~125 TFLOPS tensor | No, offload forced | 25 to 40 s |
| Tesla V100 32 GB | 32 GB | ~125 TFLOPS tensor | Yes | 10 to 18 s |
| RTX 4090 | 24 GB | ~165 TFLOPS tensor (dense) | Yes | 6 to 12 s |
| A100 40/80 GB | 40/80 GB | ~312 TFLOPS tensor | Yes | 4 to 8 s |
| H100 80 GB | 80 GB | ~756 TFLOPS tensor (dense) | Yes | 2 to 5 s |

**Every figure in the last column except the first is an estimate, not a
measurement**, and the ranges are deliberately wide. Diffusion rarely scales
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

## Why `min_vram_gb` is 24

`min_vram_gb` documents the **full residency** requirement, and full residency
OOMs on this card, so its true value cannot be measured here. 24 is bounded
below by the measurement that 15.98 GiB is insufficient and is otherwise a
conservative estimate. Any value from 18 upward behaves identically on this
hardware: the ladder selects `model_offload` either way. Pinning it exactly
needs a card with more than 16 GB.

The value that matters is not the estimate itself but what it makes the ladder
do. At 24, the existing 0.55 largest-component fraction sets a 13.2 GB
threshold for the model-offload rung, against a measured peak of 12.09 GB.
Correct, with margin. That is why the proposed
`largest_component_vram_gb` manifest override was not added: the heuristic it
would have replaced makes the right call.

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
