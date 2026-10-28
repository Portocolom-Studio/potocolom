# Design system

Read this before adding or changing any UI: a view, a section, a button, a
colour or an animation. It records the visual language and the rules the
studio, landing and auth pages share, and where each lives in the code. When
a change alters a token, a component contract or a rule below, update this
file in the same PR.

## Two surfaces

The frontend has two visual surfaces. Keep them apart.

| Surface | Routes | Tokens | Scope |
|---|---|---|---|
| Studio | `/app` | `frontend/src/app.css`, block `:root:has([data-studio-shell])` | Applies only while the studio shell is mounted, so menus, dialogs and toasts rendered outside the page pick it up too |
| Landing and auth | `/`, `/login`, `/join`, legal pages | `frontend/src/landing-tokens.css` (`.landing-surface`, `--k-*` variables) and component styles | Lato type, its own dark and light modes |

The base `:root` block in `app.css` holds the older navy shadcn values. Do not
build new studio UI on them; the studio block overrides them.

## Principles

1. **Neutral by default.** Greys carry the interface. Colour means something:
   a status, a selection, or an action that spends GPU time.
2. **One brand action per screen.** Brand blue (`bg-brand`) is reserved for
   the action that queues GPU work: Generate, Upscale, Connect, Update a live
   prompt. Ordinary primary actions use the white `default` button.
3. **Numbers line up.** Every number uses tabular figures (the studio body sets
   `tabular-nums`). Units are smaller and muted next to their value.
4. **No decoration.** No gradients, glows or coloured fills on chrome. Model
   marks are a neutral square with an initial; avatars are neutral circles.
5. **Show real data or nothing.** Never ship sample numbers in production
   views. If the API does not provide a value, leave the field out.
6. **Keyboard and screen reader first.** Every control is reachable, labelled
   and announced; see Accessibility.

## Tokens (studio)

Use the Tailwind class, not the raw value.

| Token | Class | Value | Use |
|---|---|---|---|
| `--background` | `bg-background` | `#0a0a0a` | Page |
| `--panel` / `--card` | `bg-panel`, `bg-card` | `#0f0f0f` | Sidebar, inspectors, cards |
| `--raised` / `--popover` | `bg-raised`, `bg-popover` | `#161616` | Buttons, menus, popovers |
| `--hover` / `--accent` | `bg-hover`, `bg-accent` | `#1f1f1f` | Hover, selected rows and items |
| `--field` | `bg-field` | `#0b0b0b` | Input backgrounds |
| `--border` | `border-border` | `#1f1f1f` | Hairlines between regions |
| `--line-strong` / `--input` | `border-line-strong` | `#2e2e2e` | Control borders |
| `--foreground` | `text-foreground` | `#ededed` | Primary text |
| `--muted-foreground` | `text-muted-foreground` | `#a1a1a1` | Secondary text, labels |
| `--dim` | `text-dim` | `#707070` | Tertiary text, units, captions |
| `--brand` | `bg-brand`, `text-brand` | `#3a64f0` | GPU actions, chart series 1 |
| `--brand-soft` | `bg-brand-soft` | brand at 14% | Selected brand surfaces |
| `--ring` | `ring-ring` | `#6e96f8` | Focus rings |
| `--destructive` | `text-destructive` | `#f85149` | Errors, destructive actions |

### Status colours

| Token | Value | Meaning |
|---|---|---|
| `--status-success` | `#3fb950` | Completed, active, online |
| `--status-running` | `#4493f8` | Running, informational |
| `--status-warning` | `#d29922` | Queued, waiting, attention |
| `--status-error` | `#f85149` | Failed, refused |
| `--status-neutral` | `#707070` | Idle, unknown |

Show a status as an outlined pill with a dot: `.tag` plus `.tag-success`,
`.tag-info`, `.tag-warning` or `.tag-danger`. A live state in a header is a
dot and plain text ("Active · 84 ms p95"), not a filled badge.

### Chart palette

`--chart-1` is brand blue for the main series and `--chart-2` is grey `#8a8a8a`
for a comparison series. Slots 3 to 6 (`#2fb5a3`, `#a371f7`, `#e26d9a`,
`#b08d57`) are only for categorical charts with more series. A chart colour
must never equal a status colour, so a series is never read as a state.

## Type

| Role | Font | Size and weight |
|---|---|---|
| Studio UI | Geist (self-hosted via `@fontsource/geist`) | 13px / 1.5 body, 12px labels, 11px captions |
| Page title (`h1`) | Geist | 20px, 600, letter-spacing -0.02em |
| KPI value | Geist | 22 to 24px, tabular figures |
| Code, IDs, seeds, hashes | Geist Mono | 0.92em of the surrounding text |
| Wordmark | Lato 900 | Landing and footer only |
| Landing and auth copy | Lato | Per landing component |

Headings use `text-wrap: balance`; body copy uses `text-wrap: pretty`. Fonts
are bundled, so the CSP keeps `font-src 'self'`; do not add font CDNs.

## Shape, borders and elevation

- Radius: 6px on controls (`rounded-md`), 10px on cards and panels
  (`rounded-xl`), full round only for dots, swatches, avatars and pills. The
  landing keeps its own radii.
- Where the browser supports `corner-shape: squircle`, cards, buttons and
  inputs get continuous corners automatically (an `@supports` block in
  `app.css`). Do not emulate it elsewhere.
- Borders are solid 1px: `border-border` between regions,
  `border-line-strong` around controls.
- Shadows appear only on floating layers (popovers, menus, toasts, the Focus
  prompt bar). Flat surfaces separate with borders, not shadows.

## Spacing and layout

Spacing follows a 4px grid. Common values: 8px between related controls,
12px inside compact rows, 14 by 16px section padding, 16 to 24px page padding.

### Shell

- Sidebar: 16rem wide; collapses to a 3rem icon rail with Cmd/Ctrl+B; a sheet
  on phones (swipe to dismiss).
- Top bar: 52px. Breadcrumb on the left; the view's tabs, toggles and actions
  register through `useStudioHeader()` in `src/lib/studio-chrome.svelte.ts`.
- Worker status card and the user menu sit at the bottom of the sidebar.

### Page templates

Pick the closest template before inventing a layout.

| Template | Example | Shape |
|---|---|---|
| Workbench | Realtime canvas | Tool rail, two square panes, 312px inspector, bottom bar |
| Focus | Realtime canvas, Focus mode | One large stage, floating tools and prompt bar |
| Parameter panel and viewer | Generate, Image to image, Upscale | 340px parameter column, viewer, metadata column, history strip |
| Collection | Images | Filter pills, Grid or Lineage, inspector panel |
| Table and detail | Models, Admin users | Filterable table; selecting a row opens a detail panel |
| Dashboard | Metrics | KPI row, charts, tables; tabs in the top bar |
| Settings | Account | Sub-navigation on the left, sections of label, description and action rows |

## Components

Components live in `src/lib/components/ui` (shadcn-svelte on bits-ui). Use
them rather than hand-rolled markup.

- **Button** (`ui/button`): `default` (white primary), `brand` (GPU action),
  `outline`/`secondary`, `ghost`, `destructive`. Height 30px; 36px for a
  panel's main action. Icon buttons are 30px square.
- **Fields**: `bg-field border-line-strong`, 32px high, label above in 12px
  muted text, hint or error below. Number fields show their range.
- **Images filter chips** (`image-filter-controls.svelte`): model, date and
  longest-side size choices use the shared input and select `chip` variants:
  26px high with 12px text. Keep the last result visible while a replacement
  loads; show skeletons only when there are no rows yet.
- **Benchmark charts**: static reports paint their final bars and paths without
  replaying a draw animation when a tab mounts. Keep tooltips, axes and legends.
- **Segmented control** (`ui/toggle-group`): two to five mutually exclusive
  options (Size, Generate / Image to image / Upscale, Workbench / Focus).
- **Slider** (`param-slider-field`): label and value on one line, track,
  min and max underneath.
- **Tag** (`.tag` classes): status and capability labels.
- **Kbd** (`ui/kbd`): keyboard hints. Format keys with `formatShortcut()`
  from `src/lib/shortcuts.ts` so Mac shows the command symbol and other
  platforms show Ctrl.
- **Tooltip** (`ui/tooltip`): names for icon-only controls and shortcut
  hints. Not for essential information.
- **Skeleton** (`ui/skeleton`): shaped like the content it replaces, inside a
  `role="status"` region with sr-only loading text. Stops pulsing under
  reduced motion.
- **Toast** (`ui/sonner`, helpers in `src/lib/studio-toasts.ts`): short
  confirmations and errors. Reversible destructive actions use
  `offerUndo(message, undo)` instead of a confirm dialog. Security-sensitive
  admin and account actions keep their confirm dialogs.
- **Icons**: lucide (`@lucide/svelte`), 16px, stroke 1.75, `aria-hidden`
  with a text or sr-only label next to them. Model capabilities map to icons
  in `model-panel.svelte` (`CAPABILITY_ICONS`): text to image Type, image to
  image Layers, upscale Maximize2, realtime Zap. Add new capabilities there.
- **Empty states**: one muted sentence that says what will appear and how to
  make it appear, plus the action if there is one.

## Adding a studio view

1. Add the view to `SHELL_VIEWS` (and tabs, if any) in `src/lib/studio-view.ts`.
   Views are history entries driven by `?view=` and `&tab=`.
2. Gate it with `sectionNeeds()` in `src/lib/account-display.ts`. Hidden or
   locked entries show a lock icon with the reason; never remove a control
   silently for a role that might gain access.
3. Add the sidebar entry in `nav-studio.svelte` under Create, Library or
   Platform. If the view is heavy, load it lazily through
   `src/lib/studio-prefetch.ts` so hovering the entry prefetches it.
4. Register header tabs and actions with `useStudioHeader()`, and
   view-specific command palette entries with `useViewCommands()`.
5. Add every string to both `src/lib/i18n/en.json` and `es.json`.
6. Show skeletons while loading, an empty state when there is no data, and an
   error state with a retry. Cache refetched data with `studioCache` from
   `src/lib/swr-cache.ts` so a revisit shows the last value at once. Keep cached
   private data within the active owner epoch. Persist small view choices through
   the bounded owner-scoped preferences; explicit URLs and server roles win.
7. Follow the templates, tokens and rules above, then run
   `make verify-frontend`.

## Interaction patterns

| Pattern | Where it lives |
|---|---|
| Command palette (Cmd/Ctrl+K) | `studio-command.svelte`, `useViewCommands()` |
| Keyboard shortcuts and hints | `src/lib/shortcuts.ts`; shortcuts never fire while typing (`isTypingTarget`) |
| Undo instead of confirm | `offerUndo()` in `studio-toasts.ts` |
| Optimistic updates | Update local state first, roll back and toast on failure (see star toggling) |
| Stale-while-revalidate | `studioCache`; models and history also refresh on focus, at most every 30s. Owner or role changes clear private view data |
| Remembered views | Metrics ranges/report choice and Images filters, mode, position and validated selection; versioned bounded browser preferences per confirmed owner |
| Prefetch | `prefetchView()` on hover and focus |
| Blurred placeholders | `revealOnLoad()` in `src/lib/image-reveal.ts`: thumbnail under the full image |
| View transitions | `startStudioViewTransition()` / `withViewTransition()`; exclude the live canvas and Metrics tab-only changes |
| Benchmark changes | One keyed report pane fades from 40 percent opacity and rises 4px over 200ms; keep SVG geometry static and selector focus stable; disable the effect under reduced motion |
| Back closes overlays | `pushOverlay()` / `popOverlay()` in `src/lib/overlay-history.ts` for the inspector, palette and mobile sidebar |
| Grid grab scroll | `dragScroll()` in `src/lib/drag-scroll.ts`; mouse only, native touch and keyboard scrolling |
| Swipe to dismiss | `attachSwipeDismiss()` in `src/lib/swipe-dismiss.ts` for sheets and the mobile inspector |
| Interruptible motion | Overlays use CSS transitions, not one-shot keyframes |
| Hit areas | `.hit-slop` gives small controls a 40px pointer target without overlapping neighbours |
| Safe areas | `viewport-fit=cover`; fixed edges pad with `env(safe-area-inset-*)` |
| One-time codes | `autocomplete="one-time-code"` on every second-factor field |

## Landing and auth

- **Particle sea**: `ParticleSea.svelte`, shared by the footer and the auth
  pages. A grid of particles rolls with seeded Perlin noise under a blue
  sunrise (`#3a64f0` at its heart) and recedes to a horizon; the pointer leans
  the camera and drops ripples on the water. WebGL2 computes the waves in the
  vertex shader; Canvas 2D draws a coarser grid at 30 fps. The glow and the
  fade back to `#07090f` are CSS, so the page still has its sunrise when no
  canvas starts.
- **Footer**: every page uses the same `SiteFooter.svelte` with the sea's
  `footer` variant. The home page keeps it inside its entrance gate; the root
  layout supplies it on all other pages, including auth and the studio, and
  on error pages even when the failed route is home.
  Each page has one footer. Its Lato font and dark colours are self-contained.
  The horizon
  sits just above the wordmark, which is `#07090f` so it reads as a silhouette
  against the glow; the bottom row is `#a6abb5` on the dark page and the
  particles dim behind it. A dark translucent backing keeps the copyright
  and back-to-top row readable above bright water. The studio has its own
  stacking context so its fixed sidebar cannot cover the footer.
  `npm run test:footer` checks contrast over the live
  sea and over the fallback, focus, footer style parity, page changes,
  studio footer access and phone-width overflow.
- **Language buttons**: the shared `LanguageToggle.svelte` uses local SVG
  flags for English and Spanish, with language names, keyboard controls and
  a selected state. The selected language remains stored across page loads.
- **Sign in and join**: `AuthSplit.svelte` with the sea's `field` variant,
  the horizon at 42% of the field's height. The
  waitlist is a tab on `/login` (`WaitlistForm.svelte`), shown only when
  `PUBLIC_WAITLIST_URL` is set. The landing and document header buttons open
  this tab when it is configured; otherwise they open sign-in. The login
  field omits the open-source kicker.
- **Install command**: the landing uses
  `curl -fsSL https://potocolom.leonfuller.com/install.sh | bash`. The marketing
  site's static redirect sends this URL to the latest published GitHub
  release installer. The installer and its version checks remain in the release.

## Motion and performance

- Durations: 120 to 200ms for hover and press, 200 to 300ms for panels and
  sheets, ease-out for entering and ease-in for leaving.
- `prefers-reduced-motion: reduce` turns off view transitions, crossfades,
  skeleton pulses and animated backgrounds, which render one still frame.
- Canvas and WebGL backgrounds: cap the device pixel ratio at 2, cap the frame
  rate where the effect allows (Canvas 2D fallbacks run at 30 fps), pause when
  off screen or in a hidden tab, start lighter on phones and low-memory
  devices, and only ever reduce particle counts when frames drop. Free GL
  resources on destroy. Never add a visible fps meter to production pages.

## Accessibility

- Text meets WCAG AA contrast against its actual background, including
  canvas and fallback backgrounds.
- Focus is always visible (`ring-ring`); never remove an outline without a
  replacement.
- Pointer targets are at least 40px, using `.hit-slop` when the visual is
  smaller.
- Use the ARIA pattern that matches the behaviour: tabs with arrow keys use
  the tab pattern; groups of independent buttons use `role="group"` with
  `aria-pressed`, not `radiogroup`.
- A locked control stays focusable with `aria-disabled` and explains why in
  a tooltip and sr-only text.
- Icon-only controls have an accessible name; decorative icons are
  `aria-hidden`.
- Moving focus: opening an inspector or dialog moves focus into it; closing
  returns focus to the control that opened it.
