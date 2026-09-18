# Diffusion Illusions

Public study for the `/illusions` page (issue #121). The optimizer CLI
is on PR #118. That branch is not merged. Product defaults are unchanged.
A typeset paper can follow. This document is the public study.

Figures on `/illusions` are webp exports built by
`scripts/render-illusion-figures.py`. The script writes one HTML page
per figure with an inline SVG. Real run photos are embedded as base64
data URIs. Headless Chrome screenshots each page at scale 2, and PIL
writes the webp. Regen with
`python3 scripts/render-illusion-figures.py`, which writes straight
into `frontend/static/illusions/`. Add `--only sds recipe` for one or
two figures and `--out <dir>` to write somewhere else. Photos read from
`frontend/static/illusions` and from the gitignored research exports
under `.local/`. Without those files, the structure still renders
with dashed wells.

<!-- figure: families -->
*Figure: the two families. Sampling-time methods such as Visual Anagrams (Geng, Park and Owens, arXiv 2311.17919) combine noise estimates from each view inside one reverse diffusion pass and train nothing. This page optimises a prime network instead: 5000 Score Distillation steps, 26 minutes on one RX 7600 XT, then a Dream round of 88 seconds. The cost buys arrangements that are not pixel permutations, which is how the overlay types work.*


The method is Burgert et al.,
[Diffusion Illusions](https://diffusionillusions.com). Optimize printable
prime images so a fixed physical arrangement of them shows a different
subject. Here the arrangement is a flip of the sheet. The primes are the
only free variables. Arrangements model real physics. They are
differentiable. A frozen diffusion model scores each derived view against
its prompt.

## Architecture

The optimizer has four parts, in dependency order. In code the parts
have these names: `FourierFeatureNetwork`, the `ILLUSIONS` flip
arrangement, and `DiffusionAdapter`. The paper calls the same parts
prime images, arrangement processes, and the frozen model
(paper Sec. 2-3).

1. Each prime is a Fourier Feature Network rendering RGB in `[0, 1]`.
   Only its weights move. The default network is 263,683 weights at
   `(1, 3, 512, 512)`. The gallery ran a smaller reference network:
   199,683 weights at `(1, 3, 256, 256)`.
2. Fixed arrangements turn primes into derived views. Flip is
   `a1(p) = p` and `a2(p) = rot180(p)`.
3. A frozen diffusion model scores those views: Score Distillation on
   frozen SD 1.5, then Dream Target SDEdit on DreamShaper LCM.
4. Only the prime network trains. Gradients do not enter the UNet.
   They do not enter the VAE decoder. They do pass the VAE encoder,
   because that is the only path from the weights to the latent.

<!-- figure: architecture -->
*Figure: flip pipeline with the elephant and swan keeper (seed 11, oil, joint). Orange marks the one trainable node. Arrangement math is paper Table 1, flip row.*


Rotate and hidden overlay exist in the paper and in the unmerged CLI. The
public page shows flip only. The reliability program measured the flip
type.

This workflow bakes the gallery keepers.

<!-- figure: workflow -->
*Figure: the bake pipeline with CLI defaults and the gallery value beside each one, ending in the three real output files.*


## The prime network

Each prime is an implicit image: a Fourier Feature Network. Take pixel
coordinates on a 512 x 512 grid in `[-1, 1]`. Send them through a fixed
random Gaussian projection (`2 x 256`, scale 10). Wrap that projection
with sine and cosine into 512 features. Map the features through a small
MLP (`512-256-256-256-3`, ReLU x3) to sigmoid RGB. That is the default.
The gallery ran `ReferenceFourierFeatureNetwork` instead: a 256 x 256
grid in `[0, 1)`, a `2 x 128` projection, 256 features, and four 1x1
convolutions with BatchNorm. The loop optimizes
those weights, not raw pixels. That keeps the signal in printable
structure. Pixel-space optimization hides the signal in high
frequencies. A printer cannot hold that noise (paper Sec. 4.3).

<!-- figure: ffn -->
*Figure: the prime network with its real output, the elephant and swan printable prime. The chain shows the reference network that painted this gallery, with the CLI default beside it (paper Sec. 4.3 motivates the FFN over pixels).*


Flip uses one prime and two views. They are identity and a 180-degree
turn. Rotate uses two primes and stacked transparencies. Hidden uses four
primes plus a product overlay. Multiplication models light through film.
Those overlay types are not on the public page.

## Score Distillation

Phase 1 uses frozen Stable Diffusion 1.5 (fp16 on GPU). The step
encodes each derived view to `(1, 4, 64, 64)` latents. It adds noise at
one shared random timestep, drawn as an integer between 2 and 98
percent of T = 1000. The frozen UNet scores the noised latents with high
classifier-free guidance: 100 by default, 60 for this gallery. All
prompt-target views share one CFG-doubled UNet forward per step
(`sds_loss_batch`), unless `--view-batch-size` splits that forward into
chunks (`sds_microbatch_backward`). The step applies the noise residual
as a gradient on those latents. It uses the
`(latent * residual.detach()).sum()` trick. That gradient updates only
the prime network. The default optimizer is Adam at 1e-3. The gallery
ran SGD at 1e-4.

The paper printed pseudocode computes an absolute residual under
`no_grad` (paper Eq. 2-3). That form has no gradient path to the image.
The authors' code uses residual-as-gradient. Every SDS implementation
uses that form.

<!-- figure: sds -->
*Figure: one batched SDS step (paper Eq. 2-3, with the residual-as-gradient correction). The coloured spine is the gradient path, and it stops at the UNet.*


## Two-phase loop

The optimizer CLI defaults are 500 Score Distillation steps and 8 Dream
Target rounds. Joint Dream is off. We baked the gallery on `/illusions`
with a research recipe: `--experimental-recipe author_reference`, 5000
SDS steps, and one Dream round.

That flag is not only a step budget. It swaps four things at once
(`optimize_illusion`), so every photo on `/illusions` came from a
different configuration than the one the CLI ships:

| Setting | CLI default | Gallery recipe |
|---|---|---|
| prime network | Linear MLP, 263,683 weights, 512 px | 1x1 convolutions with BatchNorm, 199,683 weights, 256 px |
| phase 1 optimizer | Adam, lr 1e-3 | SGD, lr 1e-4 |
| phase 2 optimizer | fresh Adam, lr 1e-3 | fresh Adam, lr 3e-3 |
| guidance G | 100 | 60 |
| SDS objective | `legacy`, no w(t) | `weighted_sds`, gradient scale 0.1 |

Read every number on a figure with that table beside it. The figures
label which column they show.

The loop creates a fresh Adam optimizer at the phase boundary. SDS
gradients run about four orders of magnitude larger than Dream Target
gradients. Without the reset, phase 2 moved loss by under 2 percent per
round. With the reset, loss moved 44 to 60 percent (issue #122). Those
figures come from a short flip run. The run used 250 SDS steps and
4 Dream Target rounds of 150 steps each.

<!-- figure: two-phase -->
*Figure: real checkpoints from the window-2 giraffe and penguin calibration smoke run: SDS steps 250 to 5000, Dream round 1, final. The loss and gradient norm below them are the recorded values.*



Phase 2 is Dream Target (paper Eq. 4-6). It uses DreamShaper LCM with
an LCMScheduler swap at guidance 2. Each round asks img2img for a
cleaner target at strength `s`. With the default 8 rounds, strength
starts high (0.95) and decays to a light polish (0.05). The gallery ran
one round, so `strength_schedule` returned `[0.95]` and never decayed.
The paper's Sec. 3.3.2 schedule walks 0.90 to 0.01; ours is shifted by
the SDEdit floor. The target stays frozen for the round.
`(1 - SSIM) + MSE` pulls the derived views toward it for 300 steps.
Then the next round re-dreams from the improved views. Extra Dream
rounds after the first made images worse.

<!-- figure: dream -->
*Figure: a real round-1 triple from the same smoke run: derived view, dreamed target, regressed view (paper Eq. 4-6).*


## Joint Dream

Independent Dream Targets can fight over the same pixels. Joint Dream
(`--dream-joint`, `sdedit_joint`) denoises both flip views together. At
every step it decodes each view's predicted image. It rotates the B
prediction into the upright frame. It averages both in pixel space
(`reconcile_flip`). It re-encodes. It steps from that consensus. The two
views become two orientations of one image. The returned targets are
two orientations of that same consensus.

The loop averages in pixel space on purpose. Encoding a turned image is
not the same as turning its latent: `encode(rot180 x)` differs from
`rot180(encode x)` by 0.78 to 0.97 relative latent error on the SD 1.5
VAE, measured on this pair. Joint Dream is an opt-in flag. It is not
the product default. It can rescue a pair whose shapes can be one
picture. It can also collapse a pair whose subjects cannot share one
picture.

<!-- figure: joint -->
*Figure: joint Dream on the elephant and swan pair at seed 11. The bottom row is the ablation: the same pair and seed one flag apart. Independent targets disagree about the shared pixels. Joint targets are one image seen two ways.*


## Math symbols

Every symbol used in the figures, in one place. Paper references are
Burgert et al. sections and equations. Code references are
`worker/worker/illusions.py` on PR #118.

| Symbol | Meaning | Source |
|---|---|---|
| p | One prime image: the printable artifact, RGB in `[0, 1]`. `(1, 3, 512, 512)` by default, `(1, 3, 256, 256)` in the gallery | paper Sec. 3.1, `FourierFeatureNetwork` |
| a₁, a₂ | Flip arrangements: a₁(p) = p, a₂(p) = rot₁₈₀(p). Fixed, differentiable, physically realizable | paper Sec. 3.2 + Table 1, `ILLUSIONS["flip"]` |
| d₁, d₂ | Derived views: what a human sees upright and inverted | paper Sec. 2, `_flip` |
| θ | Prime network weights. The only thing training updates. 263,683 by default, 199,683 in the gallery | `optimize_illusion` |
| x, y | Pixel coordinates. A 512 x 512 grid in `[-1, 1]` by default, a 256 x 256 grid in `[0, 1)` in the gallery | `FourierFeatureNetwork.image` |
| B | Fixed random Gaussian projection, scale 10. Shape 2 x 256 by default, 2 x 128 in the gallery | paper Sec. 4.3 |
| v | Fourier features: v = [sin(2πBx) ‖ cos(2πBx)]. 512-d by default, 256-d in the gallery | `FourierFeatureNetwork.forward` |
| σ | Sigmoid: squashes MLP output to RGB in `[0, 1]` | same |
| z | A view encoded to VAE latents, `(1, 4, 64, 64)` | `DiffusionAdapter.encode_latent` |
| t | Diffusion timestep: an integer drawn between 2 and 98 percent of T = 1000 | `sds_loss_batch` |
| ε | Gaussian noise added to the latent at step t | paper Eq. 2 |
| ε̂c / ε̂u | UNet noise estimate, conditioned on the prompt / unconditioned | CFG-doubled forward |
| G | Guidance scale pulling the estimate toward the prompt. 100 by default, 60 in the gallery | `--sds-guidance` |
| w(t) | Timestep weight, w(t) = 1 − ᾱt. A separate quantity from G, and the `legacy` objective drops it | `sds_timestep_weight` |
| r | Noise residual, applied as the latent gradient. `legacy`: εcfg − ε. `weighted_sds`: w(t)(εcfg − ε). `csd`: w(t)(εc − εu) | paper Eq. 3, `compute_sds_gradient` |
| r̄ | r detached: the gradient stops here, never entering the UNet | `(z * r.detach()).sum()` |
| η | Step on θ only, with a fresh optimizer per phase. Phase 1: Adam 1e-3 by default, SGD 1e-4 in the gallery. Phase 2: always a fresh Adam, 1e-3 by default, 3e-3 in the gallery | issue #122 |
| s | SDEdit strength: how far the target may drift from the view. 0.95 down to 0.05 over the default 8 rounds, and `[0.95]` alone for the gallery's single round | `strength_schedule` |
| z (Dream) | Dreamed target image, frozen for one round | paper Eq. 4, `sdedit` |
| L | Round loss: (1 − SSIM) + MSE between views and targets | paper Eq. 5-6 |
| d′ | Derived view after regressing toward the target | phase 2 loop |
| x̂ | Predicted clean image decoded during joint denoise | `sdedit_joint` |
| c | Consensus: c = (x̂A + rot₁₈₀(x̂B)) / 2, averaged in pixel space | `reconcile_flip` |

<!-- figure: symbols -->
*Figure: the same table on the page, with each symbol's range or shape beside the function that computes it. The figures spell the decoded predictions xA and xB, because no font here renders a circumflex over a Greek or italic letter.*

## What we measured

The reliability program on PR #118 recorded these results. PR #150
merged into that branch. Do not treat these as product defaults.

- A keeper is a specific pair, seed, and viewing mode. It is not a
  recipe that works every time.
- Human review is the gate. CLIP pair score ROC-AUC was 0.706. The
  bar was 0.75. So there is no automatic screen.
- The funnel was 206 baked cells, 43 at score 4 or 5, and 26 keepers.
  The 206 are 98 bases across 4 Dream arms, so the keepers are not 26
  independent runs. Score alone did not decide: a frame defect cut 16
  of the 43.
- Prompt wording is the biggest lever. Arguments do not predict it.
  Frame artifacts belong to the specific phrase. They do not
  belong to sketch versus oil as a medium.
- We kept oil for color and fewer photographic frames. We did not keep
  oil because it yielded more keepers than sketch.
- We rejected negative prompts.
- Extra Dream rounds after the first made images worse.
- 256 px primes were enough. 512 px cost about 3.3 times as much for no
  visible gain. This is the prime render resolution
  (`--prime-resolution`), not the SDS ladder (`--sds-low-res-fraction`),
  whose help text warns that 256 px SDS stalls subject formation.

These conclusions came after the gallery was baked. The keepers on
`/illusions` span both wordings and both styles.

<!-- figure: recipe -->
*Figure: the eight settings that differ between the CLI defaults and the gallery recipe, with the four measured verdicts below them.*


Gallery images are window-2 clean keepers. Scores are 4 or 5. Frames
are none or minor. The export is `window2-2026-08-clean`. The gallery
shows all 26 cells at that bar: 14 at score 5 and 12 at score 4,
grouped by pair. Some pairs earned several keepers across seeds,
styles, and arms. This is expected. A keeper is one specific cell,
not a recipe. Sixteen of twenty-six used joint Dream. The rest used
independent targets. Provenance for every card (pair, seed, style,
mode, arm, source PNG, sha256) is in
`frontend/src/lib/illusion-public-facts.ts`.

Below the gallery, `/illusions` shows a candidates tray
(`ILLUSION_CANDIDATES` in the same file). It holds 60 more cells from
older and later exports. Each cell carries its score, seed, mode, and
export as a tag. All 60 scored 4 or 5, 35 of them a 5. Some are middle
stages. Only 24 carry a frame rating: 20 none and 4 minor. The other 36
come from an export that never rated frames. Nothing there is a keeper yet. The tray exists so a human
can flip each card. A human promotes what reads clean.

<!-- figure: review -->
*Figure: the review funnel: 206 baked cells, 43 at score 4 or 5, 16 of those cut on a frame defect, 26 keepers. The photo pair is one cut cell beside one kept cell, same pair and seed. The CLIP branch is recorded, never gating.*


The seed matters as much as the recipe. One pair, one style, one budget,
three seeds, baked twice: joint Dream scored 5, 3 and 4, and independent
targets scored 0, 0 and 3 on the same three seeds. Two of those six cells
would clear the gallery bar. Four would not.

<!-- figure: seeds -->
*Figure: elephant and swan at seeds 11, 23 and 37, in joint and independent modes. Same prompts, same style, same budget. Only the seed and the Dream mode change.*


A score of 4 or 5 is not the only way through, and a high score is not a
pass on its own.

<!-- figure: failures -->
*Figure: three real cut cells with both views. One scored 2 because the turned view is confused. One scored 0 because only the turned view reads. One scored 5 and still lost, because a desk and pencils are painted into the image.*


## Print

<!-- figure: print -->
*Figure: flip fabrication with the elephant and swan keeper: print the prime, turn the sheet.*


Flip needs paper. Rotate and hidden types need transparency film and a
backlight. They are not on the public page.

## Status

No illusion job in the API (#116) and no studio designer (#117). Both
wait on the optimizer (#115 / PR #118). This document is the public
study of the research. It is not a shipping checklist.
