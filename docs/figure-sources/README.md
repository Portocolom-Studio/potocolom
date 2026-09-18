# Third-party figure sources

Images in this folder come from other people's papers. They are here
because `scripts/render-illusion-figures.py` composites them into the
`/illusions` figures, and because a reader of this repository is owed the
provenance without having to open the rendered webp.

Only a paper whose licence permits redistribution can be used. Most
arXiv submissions carry the default `arXiv.org perpetual, non-exclusive
license`, which grants arXiv the right to distribute and grants a reader
nothing. That is not enough, so those papers are redrawn from their text
instead of copied.

Check a licence from the raw abstract page, not from a rendered summary:

    curl -sL https://arxiv.org/abs/<id> | grep -o 'licenses/[a-z/.0-9-]*'

A `licenses/by/4.0/` result is usable with attribution. A
`licenses/nonexclusive-distrib/1.0/` result is not.

## illusion3d-fig1.jpg

- Source: Illusion3D: 3D Multiview Illusion with 2D Diffusion Priors,
  Figure 1.
- Authors: Yue Feng, Vaibhav Sanjay, Spencer Lutz, Badour AlBahar,
  Songwei Ge, Jia-Bin Huang.
- Paper: https://arxiv.org/abs/2412.09625
- Licence: Creative Commons Attribution 4.0 International (CC BY 4.0),
  https://creativecommons.org/licenses/by/4.0/
- Changes: cropped to the figure body, with the printed caption removed,
  and rescaled to 1500 px wide.
- Used in: the `families` figure, which carries the same credit on its
  face so the attribution travels with the image.

## Papers checked and not usable

These carry the default arXiv licence, so their figures are not copied
here. Where their ideas were useful, the figure was redrawn:

- Visual Anagrams, https://arxiv.org/abs/2311.17919
- Fourier Features, https://arxiv.org/abs/2006.10739
- DreamFusion, https://arxiv.org/abs/2209.14988
- DreamTime, https://arxiv.org/abs/2306.12422
- Factorized Diffusion, https://arxiv.org/abs/2404.11615
- SDEdit, https://arxiv.org/abs/2108.01073
- PTDiffusion, https://arxiv.org/abs/2503.06186
- ProlificDreamer, https://arxiv.org/abs/2305.16213
- NFSD, https://arxiv.org/abs/2310.17590
- SIREN, https://arxiv.org/abs/2006.09661
- NeRF, https://arxiv.org/abs/2003.08934

## Papers that are CC BY and stay available

Checked, usable, not yet needed by any figure:

- Classifier Score Distillation, https://arxiv.org/abs/2310.19415
- Score Jacobian Chaining, https://arxiv.org/abs/2212.00774
- Sinusoidal positional encoding, https://arxiv.org/abs/2407.09370
