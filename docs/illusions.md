# Diffusion Illusions: implementation guide

How the illusion optimizer works, how to run and tune it, and where to take
it next. Implements Burgert et al., "Diffusion Illusions: Hiding Images in
Plain Sight" (https://diffusionillusions.com). Code:
`worker/worker/illusions.py`; tests: `worker/tests/test_illusions.py`;
issues: #115 (this optimizer), #116 (jobs over the protocol), #117 (studio
designer). Milestone M8.

## The idea in one paragraph

Optimize a set of "prime" images so that fixed physical arrangements of
them (flipping the sheet, rotating a transparency on top of another,
stacking four transparencies on a backlight) each look like a different
user-chosen subject. The primes are the only free variables; the
arrangements model real physics and are differentiable; a frozen diffusion
model scores how much each arranged ("derived") image looks like its
prompt. Print the primes and the illusion works in the real world.

## Architecture

One module, four parts, in dependency order:

1. `FourierFeatureNetwork` - each prime is an implicit image: a fixed
   random Gaussian projection of pixel coordinates through sin/cos into a
   small MLP ending in sigmoid. Optimizing MLP weights instead of raw
   pixels biases the result toward printable, alignment-tolerant structure
   (paper Sec. 4.3: pixel-space optimization hides the signal in high
   frequencies and breaks physically). `image(resolution)` renders
   `(1, 3, H, W)` in `[0, 1]`.
2. Arrangements - pure functions in `ILLUSIONS` (paper Table 1):
   - `flip`: 1 prime -> `[p, rot180(p)]`, weights `[1, 1]`.
   - `rotate`: 2 primes -> `tanh(2 * p1 * rot90(p2, j))` for j in 0..3.
   - `hidden`: 4 primes -> the primes themselves plus
     `tanh(3 * p1*p2*p3*p4)`, weights `[1, 1, 1, 1, 3]`.
   Multiplication models light through stacked transparencies; the
   brightness constant k and tanh keep dynamic range (Appendix A.1). All
   are fixed, differentiable, and physically realizable - the three
   properties the paper requires of an arrangement.
3. `DiffusionAdapter` - the only place that touches diffusers. Loads one
   text2img pipeline (SD 1.5 by default, fp16 on GPU), derives the img2img
   pipeline from it, caches prompt embeddings, and exposes exactly two
   operations:
   - `sds_loss` / `sds_loss_batch`: Score Distillation. Encode derived
     image(s) to latents, noise at one shared random timestep in
     [0.02, 0.98] of the schedule, run the frozen UNet once with
     classifier-free guidance over the whole batch, and apply the guided
     noise residual as each latent's gradient via the
     `(latent * residual.detach()).sum()` trick. IMPORTANT: the paper's
     pseudocode (`abs(noise - pred_noise)` computed under `no_grad`) has
     no gradient path to the image; the residual-as-gradient form is what
     their actual codebase (Peekaboo) and every SDS implementation use.
   - `sdedit`: img2img at a given strength - noise the current derived
     image, denoise toward the prompt - producing a Dream Target.
4. `optimize_illusion` - the two-phase loop:
   - Phase 1, Score Distillation (default 500 steps): weighted SDS for all
     prompt-target derived images in one batched UNet forward per step;
     image targets (e.g. a QR code) use SSIM+MSE instead.
   - Phase 2, Dream Target (default 8 rounds x 300 steps): per round,
     freeze a target for each derived image via SDEdit at a strength that
     decays from 0.90 toward 0.05, then regress derived images to their
     targets with `(1 - SSIM) + MSE`. Decaying strength means early rounds
     reimagine freely and late rounds only polish - this is what pulls the
     compromise images SDS produces into clean subjects.
   The Adam optimizer (lr 1e-3) updates only FFN weights. Nothing ever
   backpropagates through the UNet or VAE decoder.

Module conventions: `illusions.py` is the only file in the worker package
that imports torch at module level - nothing else imports it, so the
worker still runs without the inference extra. Tests `importorskip` torch
and cover only the CPU-verifiable math.

## Running it

```bash
cd worker
TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 .venv/bin/python -m worker.illusions \
  --type flip \
  --prompt "an oil painting of a dog" \
  --prompt "a sloth hanging from a branch" \
  --out out/flip
```

- `--type flip|rotate|hidden`; give exactly one `--prompt` per derived
  image in arrangement order (flip 2, rotate 4, hidden 5 - or 4 prompts
  plus `--target-image qr.png` for the hidden slot).
- Full budget (defaults): 500 SDS + 8x300 DT, roughly 30-60 min on the
  RX 7600 XT. Smoke: `--sds-steps 20 --dream-rounds 1 --dream-steps 20`
  runs in about a minute once the model is cached.
- The ROCm env var enables fused SDPA on RDNA3 (same as the worker sets).
- Outputs: `prime_N.png` (what you print) and `derived_N.png` (what each
  arrangement should look like, including the simulated overlay).

## Tuning knobs, in order of leverage

| Knob | Default | Effect |
|---|---|---|
| prompts | - | Biggest lever by far (paper Sec. 4.2). Style-rich prompts ("an oil painting of...") work much better than bare nouns; keep all prompts in one consistent style. |
| `--sds-guidance` | 100 | DreamFusion-style high CFG. Lower (30-50) gives softer, less saturated primes; too low never converges on a subject. |
| `--sds-steps` | 500 | More helps monotonically (paper Fig. 12) but with diminishing returns after ~1000. |
| `--dream-rounds` / `--dream-steps` | 8 x 300 | More rounds with a finer strength schedule = cleaner final subjects. The paper's full schedule walks 0.90 to 0.01. |
| learning rate | 1e-3 (Adam) | In `IllusionConfig`; raise to 3e-3 for faster early structure at some stability cost. |
| seed | 0 | Different seeds give genuinely different compositions; cherry-picking across 3-4 seeds is normal for showpieces. |

Failure modes: all-gray or single-color primes mean the SDS phase was too
short or guidance too low; oversaturated neon means guidance too high or
too many SDS steps without a Dream Target phase; a hidden image bleeding
into its primes means the weight-3 prioritization needs the full Dream
Target schedule to resolve.

## Fabrication

Flip: print `prime_1.png` on paper. Rotate/hidden: print primes on
transparency film (laser printer), laminate, stack over any backlight -
the paper's authors used an old LCD backlight; a bright window works.
Ordering does not matter (multiplication commutes). A thin film of water
between sheets reduces the needed backlight strength.

## Enhancement roadmap

Ordered by value-for-effort; none are speculative seams, all come from
observed limits of the current code or explicit paper follow-ups:

1. ~~Batch the derived images through the UNet in one forward pass per SDS
   step~~ - done (`sds_loss_batch`). On the hidden illusion that was a ~5x
   cut in UNet calls - the dominant cost.
2. Composite sheet output: one PNG grid of primes + derived + simulated
   arrangement for quick visual triage of runs.
3. Negative prompts in `embed()` (trivial in diffusers) - the paper's
   baseline comparisons suggest it reduces subject bleed between derived
   images.
4. Timestep annealing for SDS (sample high timesteps early, low late) -
   standard DreamFusion-family improvement, cheap to add where the
   timestep is drawn.
5. SDXL backbone: already reachable via `--model`, but needs fp16 VAE
   care (the worker's manifest machinery solved this for generation - see
   `vae` handling in the engine) and likely gradient accumulation on
   16 GB with 5 derived images.
6. Latent-space Dream Targets: regress latents instead of pixels for the
   SSIM/MSE inner loop; faster per step but changes the loss balance -
   benchmark before adopting.
7. Rotation-overlay generalization to arbitrary angles (the paper models
   90-degree steps; `torch.rot90` would become a grid_sample rotation,
   still differentiable) - unlocks continuous-spin animations for the
   studio designer (#117).
8. Print color calibration: the paper's Figs. 15-16 show hue shifts after
   printing; a fixed printer color profile applied inside the arrangement
   (a 3x3 color matrix) would let the optimizer compensate.
9. Job integration (#116) and the studio designer (#117) - the module's
   `optimize_illusion(config, progress)` signature was shaped so the
   worker job wrapper only adds cancellation checks and asset encoding.

## Paper-to-code map

| Paper | Code |
|---|---|
| Sec. 3.1 prime images, Sec. 4.3 FFN | `FourierFeatureNetwork` |
| Sec. 3.2 + Table 1 arrangements | `ILLUSIONS`, `overlay`, `rot90` |
| Sec. 3.3.1 Score Distillation (Eq. 2-3) | `DiffusionAdapter.sds_loss_batch` |
| Sec. 3.3.2 Dream Target (Eq. 4-6), Fig. 6 | `DiffusionAdapter.sdedit` + phase 2 of `optimize_illusion` |
| Sec. 3.3.3 visual prompts | `IllusionConfig.target_image` |
| Appendix A.1 brightness constant | `overlay(brightness=...)` (k=2 rotate, k=3 hidden) |
| Appendix A.4 pseudocode | `optimize_illusion` (with the SDS gradient corrected as noted above) |
