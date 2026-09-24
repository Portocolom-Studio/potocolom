# Figure style

The house style for the `/illusions` figures, and for the figures of the
LaTeX whitepaper that will follow. Each `.drawio` file in this folder is the
source of truth; `scripts/render-illusion-figures.py` only exports it.

## Visual language

- The card style for most figures, in a light and a dark twin. Paper-grey ground
  (`#f5f5f5`), white rounded cards with a thin ink stroke, photos with a
  light border. A figure borrowed from another style is redrawn in this one.
- Three figures are process diagrams in the `/whitepaper` style instead,
  chosen per figure: `families`, `two-phase` and `review`. See "Whitepaper
  style" below.
- Lato for every piece of text, including table headers and labels. Math is
  the one exception: it renders in the TeX font, like a paper.
- Colour carries meaning, never decoration:
  - blue `#005ee3`, tint `#e6effc`: what trains, the gradient, the gallery
    column
  - grey: input and notes
  - red `#b4232a`: a stop only, where the gradient is cut or the defect a
    reviewer saw
- A figure opens with its title, and the legend sits on the same row at the
  right. No small upper-case label above the title.

## Dark theme

Every figure ships twice: `<name>.webp` for the light theme and
`<name>-dark.webp` for the dark one, and the page shows the one that matches
its theme toggle. Draw only the light figure. The export script derives the
dark twin, so the two cannot drift apart.

The dark palette follows the dark diagram style of cursor.com and
anthropic.com: a warm near-black ground, quiet borders, light text.

| role                       | light     | dark      |
| -------------------------- | --------- | --------- |
| ground                     | `#f5f5f5` | `#141413` |
| card fill                  | `#ffffff` | `#1c1b18` |
| raised fill (input, chips) | `#e4e7ec` | `#24231f` |
| note fill                  | `#eeeff2` | `#1f1e1b` |
| text                       | `#2d3142` | `#edecec` |
| secondary text             | `#4f5d75` | `#b3b1ac` |
| tertiary text              | `#7a8399` | `#8a8883` |
| card border                | `#2d3142` | `#4a4943` |
| hairline border            | `#c9cdd6` | `#34332e` |
| drawn line, curve, axis    | `#2d3142` | `#dcdad5` |
| connector                  | `#4f5d75` | `#8e8c86` |
| accent                     | `#005ee3` | `#79a8ff` |
| accent tint                | `#e6effc` | `#172238` |
| stop red                   | `#b4232a` | `#ff7d70` |

Colour maps by role, not by value. The light palette uses one ink for text,
card borders and plotted lines; on a dark ground text turns light, a card
border turns quiet, and a drawn line turns bright. A new light colour needs
its dark partner in `scripts/render-illusion-figures.py`, and the export
fails until it has one. Photos never change. Math follows the text colour.

## Whitepaper style

Some figures read best as a process, drawn like the `/whitepaper` diagrams:
big rounded lanes with a centred bold title, flowchart diamonds for
decisions, labelled coloured connectors, dashed loops, amber call-out
boxes, dark code blocks and grid tables with a navy header. Helvetica on a
white ground, plain words instead of LaTeX, a "Title - clause" heading with
one grey subtitle sentence, and no panel labels.

| meaning                          | fill      | dark twin |
| -------------------------------- | --------- | --------- |
| what trains                      | `#6366f1` | unchanged |
| frozen model, Score Distillation | `#0ea5e9` | unchanged |
| Dream stage                      | `#f59e0b` | unchanged |
| keeper, pass                     | `#10b981` | unchanged |
| cut, failure                     | `#f43f5e` | unchanged |
| input and output                 | `#1e293b` | `#334155` |
| plain step                       | `#e2e8f0` | `#2a2925` |
| lane                             | `#f8fafc` | `#1a1917` |
| call-out                         | `#fefce8` | `#2a2410` |

Saturated fills keep their colour and their white text in the dark twin;
everything else maps by role as in the card style.

## Research furniture

- Every symbol and formula is LaTeX, typeset by draw.io's MathJax: write
  `\( ... \)` inside an html label. No Unicode imitations of math.
- Multi-panel figures label their panels `(a)`, `(b)`, bold, top-left. A
  figure that is one idea gets no panel labels.
- Reference tables use booktabs rules: heavy top and bottom, a light rule
  under the header, no vertical rules. The recipe table is the exception and
  keeps its header chips and zebra rows.
- Plots have left and bottom spines, outward ticks, axis titles with the
  symbol, and no gridlines beyond a faint zero line.
- Display equations cite their source: "DreamFusion Eq. 3".
- The page numbers the captions, "Figure 3." / "Figura 3.", in reading order.

## Explanation, what has worked

- Spell a mechanism out rather than name it. The frozen-diffusion card shows
  the SDS residual and the Dream loss in full, not just their symbols.
- Prefer a horizontal flow to a vertical stack when a figure has the room.
- Draw a network as layers as well as a box chain: slab height is the
  channel width.
- Draw classifier-free guidance as vectors, with the guided estimate at
  several values of `s` along the difference.
- Tie a row of thumbnails to its curve with a dot per thumbnail.
- Circle the defect on the photo, in red, with a one-line note under it.
- Draw a physical turn as an arc with its label, between numbered steps.

## Notation

One table, used in every figure and in the whitepaper:

| symbol                                                                               | meaning                                          |
| ------------------------------------------------------------------------------------ | ------------------------------------------------ |
| `\theta`                                                                             | prime network weights, the only trained tensor   |
| `g_\theta`, `p=g_\theta(v)`                                                          | the prime network and the printed prime          |
| `\gamma(v)=[\cos 2\pi Bv,\ \sin 2\pi Bv]`                                            | Fourier features, `B_{ij}\sim\mathcal N(0,10^2)` |
| `a_1(p)=p`, `a_2(p)=R(p)`, `d_i=a_i(p)`                                              | the arrangements and the derived views           |
| `z=\mathcal E(\uparrow d)`                                                           | latent of a derived view                         |
| `z_t=\sqrt{\bar\alpha_t}\,z+\sqrt{1-\bar\alpha_t}\,\epsilon`                         | noised latent, DDPM Eq. 4                        |
| `s`                                                                                  | guidance scale, `s=1+\omega` of Ho and Salimans  |
| `\hat\epsilon_s=\hat\epsilon_\varnothing+s(\hat\epsilon_y-\hat\epsilon_\varnothing)` | guided estimate                                  |
| `w(t)=1-\bar\alpha_t`                                                                | timestep weight, never the guidance scale        |
| `\mathrm{sg}[\cdot]`                                                                 | stop-gradient                                    |
| `\tilde d=\mathrm{SDEdit}(d;t_0,y)`                                                  | Dream target; not `z`, which is the latent       |
| `\mathcal L_{\mathrm{DT}}=(1-\mathrm{SSIM})+\lVert d-\tilde d\rVert_2^2`             | Dream Target loss                                |
| `c=\tfrac12(\hat x_0^A+R(\hat x_0^B))`                                               | joint Dream consensus                            |

## For the whitepaper

Export vector figures for LaTeX from the same sources, for example
`drawio -x -f pdf -o architecture.pdf docs/figures/architecture.drawio`.
Keep the notation above, number figures in reading order, and give every
caption a bold lead phrase.
