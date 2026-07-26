<script lang="ts">
	import { resolve } from '$app/paths';
	import ForkTerminal from '$lib/components/ForkTerminal.svelte';
	import { collageImages, collageLandingSources, type CollageImage } from '$lib/collage-images';
	import { customImages, customSources } from '$lib/custom-images';
	import { favoriteImages, favoriteSources } from '$lib/favorite-images';
	import { promptMarqueePrompts } from '$lib/prompt-marquee-prompts';
	import { t } from '$lib/i18n.svelte';
	import { onMount } from 'svelte';
	import ArrowUpRightIcon from '@lucide/svelte/icons/arrow-up-right';
	import CheckIcon from '@lucide/svelte/icons/check';
	import GitForkIcon from '@lucide/svelte/icons/git-fork';
	import ParticleField from './ParticleField.svelte';
	import SalonGrid from './SalonGrid.svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const forkUrl = `${repoUrl}/fork`;
	const wall = collageImages.slice(0, 18);
	// Everything the studio has produced: the landing collage, the starred
	// generations exported from the app, and the extra exports in data/custom.
	const everything = [
		...favoriteImages.map((image) => ({
			key: image.id,
			alt: image.alt,
			...favoriteSources(image)
		})),
		...customImages.map((image) => ({
			key: image.id,
			alt: image.alt,
			...customSources(image)
		})),
		...collageImages.map((image) => ({
			key: image.file,
			alt: image.alt,
			...collageLandingSources(image)
		}))
	];

	/* Three arrangements of the same four-layer orbit, chosen by the sketch.
	   Every length is rem and has to match the .arc rules below; the whole-circle
	   shapes are additionally scaled by --orbit-scale so they fit narrow screens.

	     crown - the centre sits far below a clipped band, so only the crown of
	             each circle shows and the arcs run off the sides of the frame
	     rings - whole circles centred on the stage, with the copy inside them
	     disc  - whole circles as a medallion of their own, under the copy */
	type Shape = 'crown' | 'rings' | 'disc';

	let { shape = 'crown' }: { shape?: Shape } = $props();

	const SWAY_DEG = 3;

	// Constant arc length between neighbours, so every layer looks equally spaced.
	const SPACING = { crown: 5.4, rings: 6.2, disc: 3 };
	const RADII = {
		crown: [63, 59, 55.5, 52],
		rings: [22, 28, 34, 40],
		// Small enough that all four circles close inside one screen, under the
		// copy, and spaced by more than a tile so the rings stay separate.
		disc: [10, 7.3, 4.6, 1.9]
	};
	// Both are also in the .arc rules; the pair sets how far a tile pushes out.
	const CHIP_REM = { crown: 4.2, rings: 4.2, disc: 2.6 };
	const HOVER_REM = { crown: 8, rings: 8, disc: 5.5 };

	/* Crown only. The band is a clamp, so coverage is planned against its tallest
	   and the hover nudge against its shortest; both bounds live in the .arc rule.
	   REACH is half the widest viewport the arcs run edge to edge on - past it
	   they leave the frame, which reads better than an arc that visibly stops. */
	const BAND_MAX_REM = 21;
	const BAND_MIN_REM = 17;
	const CENTRE_REM = 65.5;
	const REACH_REM = 60;
	const SLACK_REM = 3;

	const radians = (degrees: number) => (degrees * Math.PI) / 180;

	/** Where a crown tile's centre sits, measured down from the top of the band. */
	const depthAt = (radius: number, angle: number) => CENTRE_REM - radius * Math.cos(radians(angle));

	/* Nudge an enlarged crown tile back inside the band. Tiles low on an arc
	   would otherwise grow straight through the bottom edge and come back
	   cropped. Whole circles have room around them and push outward instead. */
	function hoverLift(radius: number, angle: number): number {
		const half = HOVER_REM.crown / 2;
		const y = depthAt(radius, angle);
		if (y < half) return half - y;
		if (y > BAND_MIN_REM - half) return BAND_MIN_REM - half - y;
		return 0;
	}

	/** Every angle on a crown arc the band can actually show, sway included. */
	function crownAngles(radius: number, gap: number): number[] {
		const angles: number[] = [];
		for (let step = 0; step * gap <= 90; step += 1) {
			const angle = step * gap;
			const inner = Math.max(0, angle - SWAY_DEG);
			const outer = angle + SWAY_DEG;
			if (depthAt(radius, inner) > BAND_MAX_REM + SLACK_REM) break;
			if (radius * Math.sin(radians(inner)) > REACH_REM) break;
			// Wide arcs crest above the band; they only appear out at the sides.
			if (depthAt(radius, outer) < -SLACK_REM) continue;
			angles.push(angle);
			if (angle > 0) angles.push(-angle);
		}
		return angles.sort((a, b) => a - b);
	}

	/** A closed ring: the gap is trued up so the last tile meets the first. */
	function ringAngles(radius: number, spacing: number): number[] {
		const count = Math.max(6, Math.round((2 * Math.PI * radius) / spacing));
		return Array.from({ length: count }, (_, index) => (360 / count) * index);
	}

	const arcs = $derived.by(() => {
		const spacing = SPACING[shape];
		let poured = 0;
		return RADII[shape].map((radius, depth) => {
			const gap = ((spacing / radius) * 180) / Math.PI;
			const angles = shape === 'crown' ? crownAngles(radius, gap) : ringAngles(radius, spacing);
			const tiles = angles.map((angle) => {
				const image = everything[poured % everything.length];
				poured += 1;
				return {
					...image,
					angle,
					// Crown tiles slide vertically to stay in the band; ring tiles grow
					// outward so an inner ring never swallows what it surrounds.
					lift: shape === 'crown' ? hoverLift(radius, angle) : 0,
					push: shape === 'crown' ? 0 : (HOVER_REM[shape] - CHIP_REM[shape]) / 2
				};
			});
			return { radius, depth, tiles };
		});
	});
	const capabilities = ['live', 'gen', 'up', 'edit'] as const;
	const forkPoints = ['b1', 'b2', 'b3'] as const;
	const bullets = ['b1', 'b2', 'b3'] as const;
	const tiers = [
		{ key: 't1', price: '9', featured: false },
		{ key: 't2', price: '24', featured: true },
		{ key: 't3', price: '59', featured: false },
		{ key: 't4', price: null, featured: false }
	] as const;

	// The headline types real sample prompts, one character at a time.
	let typed = $state('');
	let promptIndex = $state(0);
	let shownTile = $state<CollageImage | null>(null);
	let orbitName = $state<string | null>(null);
	let hoveredHalf = $state<'oss' | 'cloud' | null>(null);

	onMount(() => {
		const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		if (reduced) {
			typed = promptMarqueePrompts[0].primary;
			return;
		}

		let charIndex = 0;
		let holding = 0;
		const timer = setInterval(() => {
			const full = promptMarqueePrompts[promptIndex].primary;
			if (charIndex < full.length) {
				charIndex += 1;
				typed = full.slice(0, charIndex);
				return;
			}
			holding += 1;
			if (holding < 26) return;
			holding = 0;
			charIndex = 0;
			typed = '';
			promptIndex = (promptIndex + 1) % promptMarqueePrompts.length;
		}, 55);
		return () => clearInterval(timer);
	});
</script>

<div class="krea orbit">
	<header>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<nav aria-label={t('nav.features')}>
			<a href="#does">{t('nav.features')}</a>
			<a href="#work">{t('gallery.kicker')}</a>
			<a href="#pricing">{t('nav.pricing')}</a>
			<a href="#run">{t('nav.open')}</a>
			<a href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
		</nav>
		<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
	</header>

	<main>
		<section class="stage {shape}">
			<div class="dots"><ParticleField density={0.0016} /></div>
			<div class="stage-copy">
				<h1>
					<span class="line">{t('hero.title1')}</span>
					<span class="line typing">
						<span>{typed}</span><span class="caret" aria-hidden="true"></span>
					</span>
				</h1>
				<p class="lede">{t('hero.sub')}</p>
				<div class="actions">
					<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
					<a class="pill pill-ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
				</div>
				<p class="orbit-name" aria-live="polite">{orbitName ?? ''}</p>
			</div>

			<div class="arc {shape}" aria-label={t('gallery.kicker')}>
				<div class="arc-spin">
					{#each arcs as layer (layer.radius)}
						{#each layer.tiles as tile (`${layer.radius}-${tile.angle}`)}
							<button
								type="button"
								class="chip"
								style="--a: {tile.angle}deg; --r: {layer.radius}rem; --lift: {tile.lift}rem; --out: {tile.push}rem; --depth: {layer.depth}"
								onmouseenter={() => (orbitName = tile.alt)}
								onmouseleave={() => (orbitName = null)}
								onfocus={() => (orbitName = tile.alt)}
								onblur={() => (orbitName = null)}
							>
								<!-- Without sizes the browser assumes 100vw and takes the widest
								     variant for a chip that is never bigger than HOVER_REM. -->
								<img
									src={tile.src}
									srcset={tile.srcset}
									sizes="8rem"
									alt={tile.alt}
									loading="lazy"
								/>
							</button>
						{/each}
					{/each}
				</div>
			</div>
		</section>

		<section id="does" class="jobs">
			<div class="head">
				<h2>{t('caps.title')}</h2>
				<p>{t('caps.sub')}</p>
			</div>
			<div class="jobs-grid">
				{#each capabilities as capability (capability)}
					<article>
						<h3>{t(`caps.${capability}_title`)}</h3>
						<p>{t(`caps.${capability}_body`)}</p>
					</article>
				{/each}
			</div>
		</section>

		<section id="work" class="work">
			<div class="work-wall">
				<SalonGrid tiles={wall} columns={6} rows="26svh" onactive={(next) => (shownTile = next)} />
			</div>
			<div class="work-plate">
				{#if shownTile}
					<span class="piece">{shownTile.alt}</span>
					<span class="meta">{t('gallery.kicker')}</span>
				{:else}
					<h2>{t('gallery.title_before')} {t('gallery.word_making')}</h2>
					<p>{t('gallery.sub')}</p>
				{/if}
			</div>
		</section>

		<section id="pricing" class="pricing">
			<div class="head">
				<h2>{t('pricing.title')}</h2>
				<p>{t('pricing.sub')}</p>
			</div>
			<div class="tiers">
				{#each tiers as tier (tier.key)}
					<article class:featured={tier.featured}>
						<div class="tier-head">
							<span>{t(`pricing.${tier.key}_name`)}</span>
							{#if tier.featured}
								<span class="tier-badge">{t('pricing.t2_badge')}</span>
							{/if}
						</div>
						<p class="tier-price">
							{#if tier.price}
								<span class="amount">&euro;{tier.price}</span>
								<span class="term">{t('pricing.month')}</span>
							{:else}
								<span class="amount amount-word">{t('pricing.t4_price')}</span>
							{/if}
						</p>
						<ul>
							{#each bullets as bullet (bullet)}
								<li>
									<CheckIcon aria-hidden="true" />
									{t(`pricing.${tier.key}_${bullet}`)}
								</li>
							{/each}
						</ul>
						{#if !tier.price}
							<a class="text-link" href="mailto:admin@leonfuller.com">{t('footer.contact')}</a>
						{/if}
					</article>
				{/each}
			</div>
			<p class="trial">{t('pricing.trial')}</p>
		</section>

		<section id="run" class="run">
			<h2>{t('fork.title')}</h2>
			<div class="run-body">
				<div class="run-card">
					<ul>
						{#each forkPoints as point (point)}
							<li>
								<CheckIcon aria-hidden="true" />
								{t(`fork.${point}`)}
							</li>
						{/each}
					</ul>
					<div class="actions">
						<a class="pill pill-solid" href={forkUrl}>
							<GitForkIcon aria-hidden="true" />
							{t('fork.cta_fork')}
						</a>
						<a class="pill pill-ghost" href={repoUrl}>
							{t('fork.cta_source')}
							<ArrowUpRightIcon aria-hidden="true" />
						</a>
					</div>
				</div>
				<ForkTerminal class="orbit-terminal" />
			</div>
		</section>

		<!-- The two-column close, with the pointer lighting the field behind it. -->
		<section class="split">
			<div
				class="split-half"
				onmouseenter={() => (hoveredHalf = 'oss')}
				onmouseleave={() => (hoveredHalf = null)}
				role="presentation"
			>
				<div class="dots">
					<ParticleField density={0.003} glyph={'()'} active={hoveredHalf === 'oss'} />
				</div>
				<span class="tag">{t('split.oss_p1')}</span>
				<h2>
					<span class="quiet">{t('split.oss_title')}</span>
					{t('fork.title')}
				</h2>
				<a class="pill pill-solid" href={repoUrl}>{t('fork.cta_source')}</a>
			</div>
			<div
				class="split-half"
				onmouseenter={() => (hoveredHalf = 'cloud')}
				onmouseleave={() => (hoveredHalf = null)}
				role="presentation"
			>
				<div class="dots">
					<ParticleField density={0.003} glyph={'{}'} active={hoveredHalf === 'cloud'} />
				</div>
				<span class="tag">{t('wl.kicker')}</span>
				<h2>
					<span class="quiet">{t('split.cloud_title')}</span>
					{t('wl.title')}
				</h2>
				<a class="pill pill-accent" href={resolve('/app')}>{t('wl.cta')}</a>
			</div>
		</section>
	</main>

	<footer>
		<p>{t('footer.tagline')}</p>
		<nav aria-label={t('footer.docs')}>
			<a href={repoUrl}>{t('footer.github')}</a>
			<a href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
			<a href={resolve('/benchmark')}>{t('nav.benchmark')}</a>
			<a href={resolve('/legal')}>{t('footer.legal')}</a>
			<a href={resolve('/privacy')}>{t('footer.privacy')}</a>
		</nav>
	</footer>
</div>

<style>
	/* Hallmark - macrostructure: Empty Stage - genre: spare, one motion at a time - studied DNA: antigravity.google - enrichment: pointer-lit particle field, self-typing real prompts, an arc of real work - contrast: pass - mobile: pass */
	.orbit {
		min-width: 0;
		overflow-x: clip;
		--pf-quiet: oklch(1 0 0 / 16%);
		--pf-accent: oklch(0.62 0.2 255);
	}

	:global(:root[data-krea-mode='light']) .orbit {
		--pf-quiet: oklch(0.2 0.02 262 / 22%);
		--pf-accent: oklch(0.52 0.22 258);
	}

	header {
		position: relative;
		z-index: 3;
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 1rem;
		padding: 1.25rem clamp(1rem, 4vw, 3rem);
	}

	.mark {
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.03em;
	}

	header nav {
		display: none;
		justify-content: center;
		gap: 1.6rem;
		color: var(--k-muted);
		font-size: 0.9rem;
	}

	header nav a:hover {
		color: var(--k-ink);
	}

	/* Stage ---------------------------------------------------------------- */
	.stage {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		justify-items: center;
		align-content: center;
		gap: 1.5rem;
		min-height: calc(100svh - 6rem);
		padding: clamp(2rem, 8vh, 6rem) clamp(1rem, 4vw, 3rem) 0;
		text-align: center;
	}

	.dots {
		position: absolute;
		inset: 0;
		z-index: 0;
	}

	.stage-copy,
	.arc,
	.split-half {
		position: relative;
		z-index: 1;
	}

	.stage-copy {
		display: grid;
		justify-items: center;
		gap: 1.5rem;
	}

	/* The copy sits inside the innermost ring, so it has to clear its diameter
	   rather than the viewport. */
	.stage.rings .stage-copy {
		max-width: calc(36rem * var(--orbit-scale, 1));
	}

	.stage.rings h1,
	.stage.disc h1 {
		font-size: clamp(2rem, 4.4vw, 3.4rem);
	}

	.stage.rings .lede,
	.stage.disc .lede {
		max-width: 34ch;
		font-size: 0.95rem;
	}

	/* Room above and below for the rings to close without meeting the header. */
	.stage.rings {
		min-height: calc(100svh - 4rem);
		padding-block: clamp(2rem, 6vh, 4rem);
	}

	/* Copy plus a whole medallion has to clear one screen between them. */
	.stage.disc {
		padding-block-start: clamp(1.5rem, 4vh, 3rem);
	}

	h1 {
		display: grid;
		gap: 0.2rem;
		max-width: 22ch;
		font-size: clamp(2.4rem, 6vw, 5rem);
		line-height: 1.02;
	}

	.line {
		display: block;
		min-width: 0;
	}

	.typing {
		min-height: 1.1em;
		color: var(--k-muted);
		font-weight: 500;
		overflow-wrap: anywhere;
	}

	.caret {
		display: inline-block;
		width: 0.06em;
		height: 0.9em;
		margin-inline-start: 0.06em;
		background: var(--k-accent);
		vertical-align: -0.08em;
		animation: blink 1.1s steps(1, end) infinite;
	}

	@keyframes blink {
		50% {
			opacity: 0;
		}
	}

	.lede {
		max-width: 46ch;
		color: var(--k-muted);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 0.7rem;
	}

	.arc {
		--chip-rest: clamp(2.9rem, 5.4vw, 4.2rem);
		--chip-hover: clamp(6rem, 11vw, 8rem);
		position: relative;
		width: 100%;
	}

	/* CHIP_REM and HOVER_REM in the script are these two, for the disc. */
	.arc.disc {
		--chip-rest: clamp(1.8rem, 3.2vw, 2.6rem);
		--chip-hover: clamp(4rem, 7.5vw, 5.5rem);
	}

	/* Whole circles shrink by --orbit-scale, so the tiles have to shrink with
	   them. Scaling only the radii packs full-size pictures into a smaller ring
	   until they close over whatever the ring is meant to surround. */
	.arc.rings,
	.arc.disc {
		--chip-rest: calc(clamp(2.9rem, 5.4vw, 4.2rem) * var(--orbit-scale));
		--chip-hover: calc(clamp(6rem, 11vw, 8rem) * var(--orbit-scale));
	}

	.arc.disc {
		--chip-rest: calc(clamp(1.8rem, 3.2vw, 2.6rem) * var(--orbit-scale));
		--chip-hover: calc(clamp(4rem, 7.5vw, 5.5rem) * var(--orbit-scale));
	}

	/* Whole-circle shapes are laid out in rem, so they get scaled down rather
	   than reflowed; the crown's geometry is viewport-independent by design.
	   It lives on .stage because the copy inside the rings is sized off it too. */
	.stage {
		--orbit-scale: 1;
	}

	/* The rings barely shrink: their clear centre still has to cover a copy block
	   that gets taller as it narrows, and a smaller ring closes over it. The disc
	   shrinks harder because the whole medallion has to fit the screen. */
	@media (max-width: 64rem) {
		.stage.rings {
			--orbit-scale: 0.86;
		}

		.stage.disc {
			--orbit-scale: 0.9;
		}
	}

	@media (max-width: 40rem) {
		.stage.rings {
			--orbit-scale: 0.74;
		}

		.stage.disc {
			--orbit-scale: 0.76;
		}
	}

	/* Crown: a clipped strip with the circle centre far below it, so only the
	   crown of each arc shows. BAND_MIN_REM and BAND_MAX_REM are these bounds. */
	.arc.crown {
		height: clamp(17rem, 34vh, 21rem);
		margin-block-start: auto;
		overflow: clip;
	}

	.arc.crown .arc-spin {
		inset-block-start: 65.5rem;
	}

	/* Rings: whole circles centred on the stage with the copy inside them. Four
	   rings wide enough to clear the headline cannot all close on a laptop, so
	   the outer two run off the top and bottom. Clipped to the stage rather than
	   left to spill, which put pictures behind the header nav. The layer ignores
	   the pointer except on the pictures themselves. */
	.arc.rings {
		position: absolute;
		inset: 0;
		overflow: clip;
		pointer-events: none;
	}

	.arc.rings .chip {
		pointer-events: auto;
	}

	.arc.rings .arc-spin {
		inset-block-start: 50%;
	}

	/* Disc: whole circles as a medallion of their own, under the copy. */
	.arc.disc {
		height: calc(2 * 11.3rem * var(--orbit-scale));
		margin-block-start: auto;
	}

	.arc.disc .arc-spin {
		inset-block-start: 50%;
	}

	.orbit-name {
		min-height: 1.3rem;
		color: var(--k-accent);
		font-size: 0.85rem;
	}

	.arc-spin {
		position: absolute;
		inset-inline-start: 50%;
		width: 0;
		height: 0;
		animation: sway 15s ease-in-out infinite alternate;
	}

	/* Whole circles turn rather than sway; a rocking ring reads as a mistake. */
	.arc.rings .arc-spin,
	.arc.disc .arc-spin {
		animation: turn 150s linear infinite;
	}

	@keyframes turn {
		to {
			transform: rotate(360deg);
		}
	}

	/* SWAY_DEG in the script has to cover this amplitude. */
	@keyframes sway {
		from {
			transform: rotate(-3deg);
		}
		to {
			transform: rotate(3deg);
		}
	}

	/* Both are registered so they interpolate as lengths, and both are what the
	   transition names. Transitioning `width` or `transform` while the value
	   arrives through an unregistered var leaves the property on its old value:
	   the custom property changes and the transition never runs. */
	@property --w {
		syntax: '<length>';
		inherits: false;
		initial-value: 4.2rem;
	}

	@property --shift {
		syntax: '<length>';
		inherits: false;
		initial-value: 0rem;
	}

	@property --grow {
		syntax: '<length>';
		inherits: false;
		initial-value: 0rem;
	}

	.chip {
		--w: var(--chip-rest);
		position: absolute;
		display: block;
		width: var(--w);
		aspect-ratio: 1;
		/* Pinning the origin to the corner and pulling back by half the current
		   size centres the chip on its anchor whatever size it is, so growing on
		   hover expands around the picture instead of dragging it down the arc. */
		margin: calc(var(--w) / -2) 0 0 calc(var(--w) / -2);
		transform-origin: 0 0;
		padding: 0;
		overflow: clip;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: var(--k-panel);
		cursor: pointer;
		/* --grow rides the radius, so it pushes a tile outward along its own
		   spoke. Counter-rotating next leaves the picture upright, which makes
		   the last translate screen-vertical, which is what --shift wants. */
		transform: rotate(var(--a)) translateY(calc((var(--r) * var(--orbit-scale) + var(--grow)) * -1))
			rotate(calc(var(--a) * -1)) translateY(var(--shift));
		transition:
			--w 260ms var(--k-ease),
			--shift 260ms var(--k-ease),
			--grow 260ms var(--k-ease),
			border-color 260ms var(--k-ease),
			box-shadow 260ms var(--k-ease),
			opacity 260ms var(--k-ease);
		opacity: calc(1 - var(--depth) * 0.1);
	}

	.chip img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	/* A turning ring would carry its pictures round with it. The counter-spin
	   runs at the same rate; a square always covers its own inscribed circle,
	   so the round mask stays filled at every angle. */
	.arc.rings .chip img,
	.arc.disc .chip img {
		animation: unturn 150s linear infinite;
	}

	@keyframes unturn {
		to {
			transform: rotate(-360deg);
		}
	}

	/* Shared section furniture ---------------------------------------------- */
	.jobs,
	.pricing,
	.run {
		display: grid;
		gap: clamp(1.75rem, 4vw, 3rem);
		max-width: 72rem;
		margin-inline: auto;
		padding: clamp(3.5rem, 9vw, 6.5rem) clamp(1rem, 4vw, 3rem);
	}

	.head {
		display: grid;
		gap: 0.9rem;
		justify-items: center;
		max-width: 56ch;
		margin-inline: auto;
		text-align: center;
	}

	h2 {
		font-size: clamp(1.9rem, 3.8vw, 3.1rem);
		line-height: 1.02;
	}

	h3 {
		font-size: 1.15rem;
		font-weight: 700;
	}

	.head p,
	.jobs-grid p,
	.trial,
	.work-plate p {
		color: var(--k-muted);
	}

	.jobs-grid {
		display: grid;
		gap: 1.75rem;
	}

	.jobs-grid article {
		display: grid;
		gap: 0.45rem;
		padding-block-start: 0.9rem;
		border-block-start: 1px solid var(--k-line);
	}

	.jobs-grid p {
		max-width: 34ch;
		font-size: 0.92rem;
	}

	/* Work ------------------------------------------------------------------ */
	.work {
		position: relative;
		display: grid;
		align-content: end;
		min-height: 78svh;
	}

	.work-wall {
		position: absolute;
		inset: 0;
		background: var(--k-paper);
	}

	.work-plate {
		position: relative;
		z-index: 2;
		display: grid;
		gap: 0.45rem;
		justify-self: start;
		width: min(38rem, calc(100% - 2rem));
		margin: clamp(1rem, 3vw, 2.5rem);
		padding: clamp(1.25rem, 3vw, 2rem);
		border: 1px solid var(--k-line);
		border-radius: 1.25rem;
		background: var(--k-panel);
		backdrop-filter: blur(24px);
	}

	.piece {
		padding-block-start: 0.65rem;
		border-block-start: 1px solid var(--k-line);
		font-size: 1.05rem;
		font-weight: 600;
	}

	.meta {
		color: var(--k-muted);
		font-size: 0.85rem;
	}

	/* Pricing ---------------------------------------------------------------- */
	.tiers {
		display: grid;
		gap: 1rem;
	}

	.tiers article {
		display: grid;
		align-content: start;
		gap: 0.9rem;
		padding: clamp(1.25rem, 2vw, 1.5rem);
		border: 1px solid var(--k-line);
		border-radius: 1.25rem;
		background: var(--k-panel);
	}

	.tiers article.featured {
		border-color: var(--k-accent);
	}

	.tier-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		color: var(--k-muted);
		font-size: 0.9rem;
	}

	.tier-badge {
		padding: 0.2rem 0.6rem;
		border-radius: 999px;
		background: var(--k-accent);
		color: var(--k-accent-ink);
		font-size: 0.72rem;
		font-weight: 700;
		white-space: nowrap;
	}

	.tier-price {
		display: flex;
		align-items: baseline;
		gap: 0.45rem;
	}

	.amount {
		font-size: clamp(1.9rem, 2.4vw, 2.3rem);
		font-weight: 800;
		font-variant-numeric: tabular-nums;
		letter-spacing: -0.04em;
	}

	.amount-word {
		font-size: clamp(1.5rem, 2vw, 1.9rem);
	}

	.term {
		color: var(--k-muted);
		font-size: 0.9rem;
	}

	.text-link {
		justify-self: start;
		color: var(--k-accent);
		font-weight: 700;
		text-decoration: underline;
		text-underline-offset: 0.25em;
	}

	/* Run -------------------------------------------------------------------- */
	/* Wider than the other blocks, and weighted toward the terminal: the clone
	   line is 74 monospace characters and was scrolling sideways inside it. */
	.run {
		max-width: 78rem;
	}

	.run-body {
		display: grid;
		gap: 1rem;
		align-items: stretch;
	}

	.run-card {
		display: flex;
		flex-direction: column;
		justify-content: space-between;
		gap: 2rem;
		min-height: 17rem;
		padding: clamp(1.25rem, 3vw, 2rem);
		border: 1px solid var(--k-line);
		border-radius: 1.25rem;
		background: var(--k-panel);
	}

	.tiers ul,
	.run-card ul {
		display: grid;
		gap: 0.9rem;
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.tiers li,
	.run-card li {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		gap: 0.6rem;
		align-items: start;
		color: var(--k-muted);
		line-height: 1.55;
	}

	.tiers li {
		font-size: 0.92rem;
	}

	.tiers li :global(svg),
	.run-card li :global(svg),
	.pill :global(svg) {
		width: 1rem;
		height: 1rem;
		flex: none;
	}

	.tiers li :global(svg),
	.run-card li :global(svg) {
		margin-block-start: 0.25rem;
		color: var(--k-accent);
	}

	.pill :global(svg) {
		margin-inline-end: 0.45rem;
	}

	.pill-ghost :global(svg) {
		margin-inline: 0.45rem 0;
		opacity: 0.7;
	}

	.run .actions {
		justify-content: flex-start;
	}

	/* Split close ------------------------------------------------------------ */
	.split {
		display: grid;
		gap: clamp(2rem, 5vw, 3rem);
		padding: clamp(4rem, 11vw, 8rem) clamp(1rem, 4vw, 3rem);
		border-block-start: 1px solid var(--k-line);
	}

	.split-half {
		position: relative;
		display: grid;
		align-content: center;
		justify-items: center;
		gap: 1.25rem;
		min-height: 20rem;
		padding: clamp(1.5rem, 4vw, 3rem);
		text-align: center;
	}

	.split-half > :not(.dots) {
		position: relative;
		z-index: 1;
	}

	.tag {
		padding: 0.25rem 0.7rem;
		border-radius: 999px;
		background: var(--k-panel);
		color: var(--k-muted);
		font-size: 0.76rem;
	}

	.split-half h2 {
		display: grid;
		gap: 0.15rem;
		max-width: 16ch;
	}

	.quiet {
		color: var(--k-muted);
		font-weight: 500;
	}

	footer {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 1rem 2rem;
		max-width: 78rem;
		margin-inline: auto;
		padding: 2rem clamp(1rem, 4vw, 3rem);
		border-block-start: 1px solid var(--k-line);
		color: var(--k-muted);
		font-size: 0.85rem;
	}

	footer nav {
		display: flex;
		flex-wrap: wrap;
		gap: 0.6rem 1.25rem;
	}

	@media (min-width: 48rem) {
		header nav {
			display: flex;
		}

		.jobs-grid {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}

		.run-body {
			grid-template-columns: minmax(0, 0.78fr) minmax(0, 1.22fr);
		}

		.split {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}

	@media (min-width: 64rem) {
		.tiers {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}
	}

	@media (min-width: 48rem) and (max-width: 64rem) {
		.tiers {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}

	/* Keyed on a picture being under the cursor, not on the band. Hovering .arc
	   froze the orbit from anywhere in the strip, including the empty sky.
	   Not behind a hover media query: the enlarge is the point of the section. */
	.arc-spin:has(.chip:hover),
	.arc-spin:has(.chip:focus-visible) {
		animation-play-state: paused;
	}

	/* HOVER_REM in the script is the largest this gets. */
	.chip:hover,
	.chip:focus-visible {
		--w: var(--chip-hover);
		--shift: var(--lift);
		--grow: var(--out);
		z-index: 5;
		border-color: var(--k-accent);
		box-shadow: 0 1rem 3rem oklch(0 0 0 / 60%);
		opacity: 1;
	}

	@media (max-width: 48rem) {
		.work {
			min-height: 0;
		}

		.work-wall {
			position: static;
		}

		.work-plate {
			margin-block-start: -2.5rem;
		}
	}

	@media (max-width: 30rem) {
		.actions {
			width: 100%;
			flex-direction: column;
		}

		.actions .pill {
			width: 100%;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.arc-spin,
		.chip img,
		.caret {
			animation: none;
		}
	}
</style>
