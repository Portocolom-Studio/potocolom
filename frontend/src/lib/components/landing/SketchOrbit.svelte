<script lang="ts">
	import { resolve } from '$app/paths';
	import ForkTerminal from '$lib/components/ForkTerminal.svelte';
	import PromptMarquee from '$lib/components/PromptMarquee.svelte';
	import { collageImages, collageLandingSources } from '$lib/collage-images';
	import { customImages, customSources } from '$lib/custom-images';
	import { favoriteImages, favoriteSources } from '$lib/favorite-images';
	import { makingImages, makingSources } from '$lib/making-images';
	import { promptMarqueePrompts } from '$lib/prompt-marquee-prompts';
	import { t } from '$lib/i18n.svelte';
	import ArrowUpRightIcon from '@lucide/svelte/icons/arrow-up-right';
	import CheckIcon from '@lucide/svelte/icons/check';
	import GitForkIcon from '@lucide/svelte/icons/git-fork';
	import LandingFaq from './LandingFaq.svelte';
	import LandingWaitlist from './LandingWaitlist.svelte';
	import LandingLoader, {
		type LandingAsset,
		type LandingEntrancePhase
	} from './LandingLoader.svelte';
	import ParticleField from './ParticleField.svelte';
	import SalonGrid, { type SalonTile } from './SalonGrid.svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const forkUrl = `${repoUrl}/fork`;
	const wall: SalonTile[] = makingImages.map((image) => ({
		key: image.id,
		alt: image.alt,
		...makingSources(image)
	}));
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

	/* Four concentric rings of studio work around the hero copy. Lengths are rem
	   and must match the .arc rules below; --orbit-scale shrinks the medallion
	   on narrow screens so the clear centre still covers the copy. */
	const SPACING = 5.5;
	const RADII = [20, 25.4, 30.8, 36.2];
	const CHIP_REM = 3.9;
	const HOVER_REM = 7.4;

	/** A closed ring: the gap is trued up so the last tile meets the first. */
	function ringAngles(radius: number, spacing: number): number[] {
		const count = Math.max(6, Math.round((2 * Math.PI * radius) / spacing));
		return Array.from({ length: count }, (_, index) => (360 / count) * index);
	}

	const arcs = $derived.by(() => {
		let poured = 0;
		return RADII.map((radius, depth) => {
			const angles = ringAngles(radius, SPACING);
			const tiles = angles.map((angle) => {
				const image = everything[poured % everything.length];
				poured += 1;
				return {
					...image,
					angle,
					// Grow outward so an inner ring never swallows what it surrounds.
					push: (HOVER_REM - CHIP_REM) / 2
				};
			});
			return { radius, depth, tiles };
		});
	});
	const orbitAssets: LandingAsset[] = $derived.by(() => {
		const unique = new Map<string, LandingAsset>();
		for (const layer of arcs) {
			for (const tile of layer.tiles) {
				const asset = { src: tile.src, srcset: tile.srcset, sizes: '8rem' };
				unique.set(`${asset.srcset}|${asset.sizes}`, asset);
			}
		}
		return [...unique.values()];
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
	let shownTile = $state<SalonTile | null>(null);
	let plateSide = $state<'start' | 'end'>('start');
	let workStage: HTMLElement | undefined = $state();
	let orbitName = $state<string | null>(null);
	let hoveredHalf = $state<'oss' | 'cloud' | null>(null);
	let entrancePhase: LandingEntrancePhase = $state('loading');

	function onWallActive(next: SalonTile | null) {
		shownTile = next;
		if (!next) {
			plateSide = 'start';
			return;
		}
		// After the lit class lands, park the plate on the opposite half so it
		// does not sit on the image under the cursor.
		requestAnimationFrame(() => {
			const stage = workStage;
			const lit = stage?.querySelector('.salon-grid button.lit');
			if (!(lit instanceof HTMLElement) || !stage) {
				plateSide = 'start';
				return;
			}
			const tileBox = lit.getBoundingClientRect();
			const stageBox = stage.getBoundingClientRect();
			const tileMid = (tileBox.left + tileBox.right) / 2;
			const stageMid = (stageBox.left + stageBox.right) / 2;
			plateSide = tileMid < stageMid ? 'end' : 'start';
		});
	}

	$effect(() => {
		if (entrancePhase !== 'ready') return;
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

<div
	class="landing-surface orbit"
	class:entrance-loading={entrancePhase === 'loading'}
	class:entrance-revealing={entrancePhase === 'revealing'}
	class:entrance-ready={entrancePhase === 'ready'}
	aria-busy={entrancePhase !== 'ready'}
>
	<LandingLoader assets={orbitAssets} onphase={(phase) => (entrancePhase = phase)} />

	<header
		class="landing-content"
		inert={entrancePhase !== 'ready'}
		aria-hidden={entrancePhase !== 'ready'}
	>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<nav aria-label={t('nav.features')}>
			<a href="#does">{t('nav.features')}</a>
			<a href="#work">{t('gallery.kicker')}</a>
			<a href="#pricing">{t('nav.pricing')}</a>
			<a href="#run">{t('nav.open')}</a>
			<a href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
			<a href={resolve('/benchmark')}>{t('nav.benchmark')}</a>
		</nav>
		<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
	</header>

	<main
		class="landing-content"
		inert={entrancePhase !== 'ready'}
		aria-hidden={entrancePhase !== 'ready'}
	>
		<section class="stage">
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

			<div class="arc" aria-label={t('gallery.kicker')}>
				<div class="arc-spin">
					{#each arcs as layer (layer.radius)}
						{#each layer.tiles as tile (`${layer.radius}-${tile.angle}`)}
							<button
								type="button"
								class="chip"
								style="--a: {tile.angle}deg; --r: {layer.radius}rem; --out: {tile.push}rem; --depth: {layer.depth}"
								onmouseenter={() => (orbitName = tile.alt)}
								onmouseleave={() => (orbitName = null)}
								onfocus={() => (orbitName = tile.alt)}
								onblur={() => (orbitName = null)}
							>
								<!-- Without sizes the browser assumes 100vw and takes the widest
								     variant for a chip that is never bigger than HOVER_REM. -->
								{#if entrancePhase !== 'loading'}
									<img
										src={tile.src}
										srcset={tile.srcset}
										sizes="8rem"
										alt={tile.alt}
										loading="lazy"
									/>
								{/if}
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
			<div class="work-stage" bind:this={workStage}>
				<div class="work-wall">
					<SalonGrid tiles={wall} onactive={onWallActive} />
				</div>
				<div class="work-plate" class:side-end={plateSide === 'end'}>
					{#if shownTile}
						<span class="piece">{shownTile.alt}</span>
						<span class="meta">{t('gallery.kicker')}</span>
					{:else}
						<h2>{t('gallery.title_before')} {t('gallery.word_making')}</h2>
						<p>{t('gallery.sub')}</p>
					{/if}
				</div>
			</div>
			<PromptMarquee />
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

		<!-- Particles under the split; waitlist keeps its LatentCanvas field. -->
		<section class="close">
			<div class="split">
				<div class="close-field" aria-hidden="true">
					<ParticleField density={0.0024} />
				</div>
				<div class="split-veil" aria-hidden="true"></div>
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
					<span class="tag">{t('split.cloud_p1')}</span>
					<h2>
						<span class="quiet">{t('split.cloud_title')}</span>
						{t('wl.title')}
					</h2>
					<a class="pill pill-accent" href="#waitlist">{t('wl.cta')}</a>
				</div>
			</div>

			<LandingFaq />
			<LandingWaitlist />
		</section>
	</main>

	<footer
		class="landing-content"
		inert={entrancePhase !== 'ready'}
		aria-hidden={entrancePhase !== 'ready'}
	>
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

	:global(body:has(.orbit.entrance-loading)),
	:global(body:has(.orbit.entrance-revealing)) {
		overflow: hidden;
	}

	.landing-content {
		opacity: 0;
		transition: opacity 760ms var(--k-ease) 480ms;
	}

	.entrance-revealing .landing-content,
	.entrance-ready .landing-content {
		opacity: 1;
	}

	:global(:root[data-landing-mode='light']) .orbit {
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
		--orbit-scale: 0.94;
		--orbit-tail: clamp(1.5rem, 3vh, 2rem);
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		justify-items: center;
		align-content: center;
		gap: 1.5rem;
		min-height: calc(92svh - 4rem + var(--orbit-tail));
		padding-block-start: clamp(1.5rem, 4.5vh, 3rem);
		padding-block-end: calc(clamp(3.5rem, 10vh, 6.5rem) + var(--orbit-tail));
		padding-inline: clamp(1rem, 4vw, 3rem);
		text-align: center;
	}

	/* The rings barely shrink: their clear centre still has to cover a copy block
	   that gets taller as it narrows, and a smaller ring closes over it. */
	@media (max-width: 64rem) {
		.stage {
			--orbit-scale: 0.82;
		}
	}

	@media (max-width: 40rem) {
		.stage {
			--orbit-scale: 0.7;
		}
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
		/* Copy sits inside the innermost ring, so it clears that diameter. */
		max-width: calc(32rem * var(--orbit-scale, 1));
	}

	.stage h1 {
		font-size: clamp(2rem, 4.4vw, 3.4rem);
	}

	.stage .lede {
		max-width: 34ch;
		font-size: 0.95rem;
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
		--chip-rest: calc(clamp(2.7rem, 5vw, 3.9rem) * var(--orbit-scale));
		--chip-hover: calc(clamp(5.5rem, 10vw, 7.4rem) * var(--orbit-scale));
		position: absolute;
		inset: 0;
		overflow: clip;
		pointer-events: none;
		width: 100%;
		opacity: 0;
		transform: scale(0.14);
		transform-origin: 50% 50%;
		transition:
			opacity 760ms var(--k-ease) 520ms,
			transform 1500ms var(--k-ease-in-out);
	}

	.entrance-revealing .arc,
	.entrance-ready .arc {
		opacity: 1;
		transform: scale(1);
	}

	.arc .chip {
		pointer-events: auto;
	}

	.orbit-name {
		min-height: 1.3rem;
		color: var(--k-accent);
		font-size: 0.85rem;
	}

	.arc-spin {
		position: absolute;
		inset-inline-start: 50%;
		inset-block-start: 50%;
		width: 0;
		height: 0;
		animation: turn 150s linear infinite;
	}

	@keyframes turn {
		to {
			transform: rotate(360deg);
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
		--shift: 0rem;
		--grow: 0rem;
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
	.arc .chip img {
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
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		height: auto;
		overflow: visible;
	}

	.work-stage {
		position: relative;
		display: block;
		width: 100%;
		height: auto;
		overflow: visible;
		container-type: inline-size;
	}

	.work-wall {
		position: relative;
		z-index: 0;
		width: 100%;
		height: auto;
		overflow: visible;
		background: var(--k-paper);
	}

	.work-plate {
		--plate-inset: clamp(1rem, 3vw, 2.5rem);
		position: absolute;
		z-index: 2;
		inset-block-end: var(--plate-inset);
		inset-inline-start: var(--plate-inset);
		display: grid;
		gap: 0.45rem;
		width: min(38rem, calc(100% - 2 * var(--plate-inset)));
		padding: clamp(1.25rem, 3vw, 2rem);
		border: 1px solid var(--k-line);
		border-radius: 1.25rem;
		background: var(--k-panel);
		backdrop-filter: blur(24px);
		pointer-events: none;
		transform: translate3d(0, 0, 0);
		transition: transform 520ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	/* Slide to the opposite edge without using inset:auto (that cannot ease). */
	.work-plate.side-end {
		transform: translate3d(calc(100cqi - 100% - 2 * var(--plate-inset)), 0, 0);
	}

	.work-plate :global(a),
	.work-plate :global(button) {
		pointer-events: auto;
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

	/* Close: particles stay in the split; waitlist brings its LatentCanvas. */
	.close {
		position: relative;
		isolation: isolate;
		overflow: clip;
		border-block-start: 1px solid color-mix(in oklch, var(--k-line) 45%, transparent);
	}

	.split {
		position: relative;
		z-index: 1;
		display: grid;
		gap: clamp(1.5rem, 4vw, 2.5rem);
		padding: clamp(3.5rem, 9vw, 6.5rem) clamp(1rem, 4vw, 3rem) clamp(1.5rem, 4vw, 2.5rem);
		overflow: clip;
	}

	.close-field {
		position: absolute;
		inset: 0;
		z-index: 0;
		pointer-events: none;
		/* Soft exit so the particle band does not hard-cut into the waitlist. */
		-webkit-mask-image: linear-gradient(to bottom, #000 0%, #000 62%, transparent 100%);
		mask-image: linear-gradient(to bottom, #000 0%, #000 62%, transparent 100%);
	}

	/* Glyph particles in each half fade with the shared field. */
	.split .dots {
		-webkit-mask-image: linear-gradient(to bottom, #000 0%, #000 58%, transparent 100%);
		mask-image: linear-gradient(to bottom, #000 0%, #000 58%, transparent 100%);
	}

	/* Upper band starts veiled for type contrast, then opens gradually. */
	.split-veil {
		position: absolute;
		inset: 0;
		z-index: 0;
		pointer-events: none;
		background: linear-gradient(
			to bottom,
			var(--k-veil) 0%,
			oklch(0.08 0.012 265 / 28%) 40%,
			oklch(0.08 0.012 265 / 8%) 72%,
			transparent 100%
		);
	}

	:global(:root[data-landing-mode='light']) .split-veil {
		background: linear-gradient(
			to bottom,
			var(--k-veil) 0%,
			oklch(0.97 0.004 255 / 32%) 40%,
			oklch(0.97 0.004 255 / 10%) 72%,
			transparent 100%
		);
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
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: oklch(0.14 0.018 265 / 55%);
		backdrop-filter: blur(10px);
		color: var(--k-muted);
		font-size: 0.76rem;
	}

	:global(:root[data-landing-mode='light']) .tag {
		background: oklch(1 0 0 / 55%);
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
		--grow: var(--out);
		z-index: 5;
		border-color: var(--k-accent);
		box-shadow: 0 1rem 3rem oklch(0 0 0 / 60%);
		opacity: 1;
	}

	@media (max-width: 48rem) {
		.work-stage {
			min-height: 0;
		}

		.work-plate,
		.work-plate.side-end {
			position: relative;
			inset: auto;
			margin: -2.5rem clamp(1rem, 3vw, 2.5rem) clamp(1rem, 3vw, 2.5rem);
			transform: none;
			transition: none;
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
		.landing-content,
		.arc {
			transition-duration: 120ms;
			transition-delay: 0ms;
		}

		.arc,
		.entrance-revealing .arc,
		.entrance-ready .arc {
			transform: none;
		}

		.arc-spin,
		.chip img,
		.caret {
			animation: none;
		}

		.work-plate {
			transition: none;
		}
	}
</style>
