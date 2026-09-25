# Figure style

The house style for every figure on the website (`/illusions`, `/whitepaper`,
and any page that follows) and for the figures of the LaTeX whitepaper to come.
Each `.drawio` file is the source of truth: `docs/figures/*.drawio` for
`/illusions`, `docs/figures/whitepaper/*.drawio` for `/whitepaper`.
`scripts/render-illusion-figures.py` only exports them, a light and a dark
webp per figure:

    python3 scripts/render-illusion-figures.py              # /illusions
    python3 scripts/render-illusion-figures.py --src docs/figures/whitepaper --out frontend/static/whitepaper

## Visual language

- The card style for most figures, in a light and a dark twin. Paper-grey ground
  (`#f5f5f5`), white rounded cards with a thin ink stroke, photos with a
  light border. A figure borrowed from another style is redrawn in this one.
- Three `/illusions` figures are process diagrams in a Tailwind palette
  instead, chosen per figure: `families`, `two-phase` and `review`. See
  "Process-diagram style" below.
- Architecture and workflow diagrams follow "Architecture diagrams" below:
  the card style plus icons.
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

| role                       | light                | dark                 |
| -------------------------- | -------------------- | -------------------- |
| ground                     | `#f5f5f5`            | `#141413`            |
| card fill                  | `#ffffff`            | `#1c1b18`            |
| raised fill (input, chips) | `#e4e7ec`            | `#24231f`            |
| note fill                  | `#eeeff2`            | `#1f1e1b`            |
| text                       | `#2d3142`            | `#edecec`            |
| secondary text             | `#4f5d75`            | `#b3b1ac`            |
| tertiary text              | `#7a8399`            | `#8a8883`            |
| card border                | `#2d3142`            | `#4a4943`            |
| hairline border            | `#c9cdd6`            | `#34332e`            |
| drawn line, curve, axis    | `#2d3142`            | `#dcdad5`            |
| connector                  | `#4f5d75`            | `#8e8c86`            |
| accent                     | `#005ee3`            | `#79a8ff`            |
| accent tint                | `#e6effc`            | `#172238`            |
| stop red                   | `#b4232a`            | `#ff7d70`            |
| good end, tint             | `#1f7a4d`, `#e3f1e8` | `#4fbf82`, `#13261b` |
| reversed end, tint         | `#b26b00`, `#fbeed6` | `#f0b454`, `#2b2112` |
| bad end tint               | `#f8e3e3`            | `#2e1616`            |

Colour maps by role, not by value. The light palette uses one ink for text,
card borders and plotted lines; on a dark ground text turns light, a card
border turns quiet, and a drawn line turns bright. A new light colour needs
its dark partner in `scripts/render-illusion-figures.py`, and the export
fails until it has one. Photos never change. Math follows the text colour.

## Process-diagram style

Some figures read best as a process, drawn like the original `/whitepaper` diagrams:
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

## Architecture diagrams

How the `/whitepaper` figures are drawn, and the pattern for any new
architecture, workflow or system diagram on the website.

- Card style, Lato, 1040 units wide, light and dark twins. No panel labels:
  a figure that needs two parts gets two titled regions, not `(a)` and `(b)`.
- Icons carry the nouns. One icon per concept, the same everywhere, drawn as
  draw.io `mxgraph.aws4` line glyphs recoloured to a house ink
  (`fillColor=<ink>;strokeColor=none`). Put a 32 to 44 unit icon at the top or
  left of its card, or let an icon with a caption replace a box for a store.

  | concept                     | shape (`mxgraph.aws4.`)           |
  | --------------------------- | --------------------------------- |
  | browser, the studio         | `client`                          |
  | API replica, relay          | `traditional_server`              |
  | scheduler, control loop     | `gear`                            |
  | GPU worker                  | `instance2`                       |
  | PostgreSQL, the ledger      | `database`                        |
  | one table, one record store | `generic_database`                |
  | object storage              | `bucket`                          |
  | queue                       | `queue`                           |
  | frames, a stream            | `multimedia`                      |
  | traffic both ways           | `internet_alt2`                   |
  | webhook                     | `http_notification`               |
  | payment, checkout           | `cost_management`                 |
  | credits, grant, balance     | `savings_plans`                   |
  | email                       | `email_2`                         |
  | retry                       | `backup_plan`                     |
  | TTL, timeout, idle          | `backup_recovery_time_objective`  |
  | rules, checklist            | `permissions`                     |
  | fails closed, secured       | `ssl_padlock`                     |
  | protection                  | `shield2`                         |
  | failure, alarm              | `backup_recovery_point_objective` |
  | document, manifest, record  | `document`                        |
  | people                      | `user`, `users`                   |

- Icon ink follows the colour rules: accent blue on the live or hot path,
  red only for a failure, ink otherwise.
- Status colours mark outcomes, and only outcomes: green `#1f7a4d` on tint
  `#e3f1e8` for a good end (committed), amber `#b26b00` on `#fbeed6` for a
  reversed or neutral end (refunded, paused), red `#b4232a` on `#f8e3e3` for a
  bad end (expired, refused). Colour the edges into them to match.
- Number the connections that need explaining with small round markers, and
  explain each in a booktabs key table under the diagram: marker, what
  crosses, how.
- Solid blue edges for the live path, dashed grey for side paths such as
  finished images; a dashed card border means designed but not built yet,
  and the legend says so. Never draw designed work as shipped.
- Every label traces to the page copy or `docs/internals`; keep each figure
  to what its section says.

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
