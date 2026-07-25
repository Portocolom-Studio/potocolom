<script lang="ts">
	import { resolve } from '$app/paths';
	import ForkTerminal from '$lib/components/ForkTerminal.svelte';
	import { collageImages, collageLandingSources, type CollageImage } from '$lib/collage-images';
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
	const arcTiles = collageImages.slice(0, 14);
	const wall = collageImages.slice(0, 18);
	// Concentric rings under the arc: the whole gallery, orbiting.
	const orbits = [
		{ radius: 9, tiles: collageImages.slice(0, 7) },
		{ radius: 15, tiles: collageImages.slice(7, 19) },
		{ radius: 21, tiles: collageImages.slice(0, 18).toReversed() }
	];
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
	let orbitTile = $state<CollageImage | null>(null);
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
			</div>

			<div class="arc" aria-label={t('gallery.kicker')}>
				<div class="arc-spin">
					{#each arcTiles as tile, index (tile.file)}
						{@const sources = collageLandingSources(tile)}
						<span class="chip" style="--i: {index}; --n: {arcTiles.length}">
							<img src={sources.src} srcset={sources.srcset} alt={tile.alt} loading="lazy" />
						</span>
					{/each}
				</div>
			</div>
		</section>

		<section id="orbit" class="orbit-gallery" aria-label={t('gallery.kicker')}>
			<div class="rings">
				{#each orbits as ring, ringIndex (ringIndex)}
					<div class="orbit-ring" style="--r: {ring.radius}rem; --spin: {26 + ringIndex * 14}s">
						{#each ring.tiles as tile, index (`${ringIndex}-${tile.file}`)}
							{@const sources = collageLandingSources(tile)}
							<button
								type="button"
								class="orb"
								style="--i: {index}; --n: {ring.tiles.length}"
								onmouseenter={() => (orbitTile = tile)}
								onmouseleave={() => (orbitTile = null)}
								onfocus={() => (orbitTile = tile)}
								onblur={() => (orbitTile = null)}
							>
								<img src={sources.src} srcset={sources.srcset} alt={tile.alt} loading="lazy" />
							</button>
						{/each}
					</div>
				{/each}
				<p class="orbit-name" aria-live="polite">
					{orbitTile ? orbitTile.alt : t('gallery.kicker')}
				</p>
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
		width: 100%;
		height: clamp(8rem, 20vh, 12rem);
		margin-block-start: auto;
		overflow: clip;
	}

	.arc-spin {
		position: absolute;
		inset-block-start: 80rem;
		inset-inline-start: 50%;
		width: 0;
		height: 0;
		animation: sway 44s ease-in-out infinite alternate;
	}

	@keyframes sway {
		from {
			transform: rotate(-3.5deg);
		}
		to {
			transform: rotate(3.5deg);
		}
	}

	.chip {
		position: absolute;
		display: block;
		width: clamp(3.2rem, 6vw, 4.6rem);
		aspect-ratio: 1;
		margin: -0.5rem 0 0 -0.5rem;
		overflow: clip;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: var(--k-panel);
		transform: rotate(calc((var(--i) - (var(--n) - 1) / 2) * 4deg)) translateY(-80rem);
		transition: transform 300ms var(--k-ease);
	}

	.chip img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	/* Orbit gallery ---------------------------------------------------------- */
	.orbit-gallery {
		display: grid;
		place-items: center;
		padding: clamp(2rem, 6vw, 4rem) 1rem;
		overflow: clip;
	}

	.rings {
		position: relative;
		display: grid;
		place-items: center;
		width: min(48rem, 100%);
		aspect-ratio: 1;
	}

	.orbit-ring {
		position: absolute;
		inset: 0;
		display: grid;
		place-items: center;
		animation: turn var(--spin) linear infinite;
	}

	.orbit-ring:nth-child(even) {
		animation-direction: reverse;
	}

	@keyframes turn {
		to {
			transform: rotate(360deg);
		}
	}

	.orb {
		position: absolute;
		width: clamp(2.6rem, 5vw, 4rem);
		aspect-ratio: 1;
		padding: 0;
		overflow: clip;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: var(--k-panel);
		cursor: pointer;
		transform: rotate(calc(var(--i) * (360deg / var(--n)))) translateY(calc(var(--r) * -1))
			rotate(calc(var(--i) * (-360deg / var(--n))));
		transition:
			width 260ms var(--k-ease),
			border-color 260ms var(--k-ease),
			box-shadow 260ms var(--k-ease);
	}

	.orb img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.orbit-name {
		position: relative;
		z-index: 3;
		max-width: 18ch;
		color: var(--k-muted);
		font-size: 0.9rem;
		text-align: center;
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
			grid-template-columns: minmax(0, 0.88fr) minmax(0, 1.12fr);
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

	@media (hover: hover) and (pointer: fine) {
		.rings:hover .orbit-ring {
			animation-play-state: paused;
		}

		.orb:hover,
		.orb:focus-visible {
			z-index: 4;
			width: clamp(6rem, 12vw, 10rem);
			border-color: var(--k-accent);
			box-shadow: 0 1rem 2.5rem oklch(0 0 0 / 55%);
		}

		.chip:hover {
			transform: rotate(calc((var(--i) - (var(--n) - 1) / 2) * 4deg)) translateY(-80.7rem)
				scale(1.22);
		}
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

	@media (max-width: 40rem) {
		.chip {
			transform: rotate(calc((var(--i) - (var(--n) - 1) / 2) * 7deg)) translateY(-80rem);
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
		.caret,
		.orbit-ring {
			animation: none;
		}
	}
</style>
