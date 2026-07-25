<script lang="ts">
	import { resolve } from '$app/paths';
	import ForkTerminal from '$lib/components/ForkTerminal.svelte';
	import HeroImageField from '$lib/components/HeroImageField.svelte';
	import { collageImage, collageImages, collageLandingSources } from '$lib/collage-images';
	import { MODEL_SPECS } from '$lib/model-specs';
	import { t } from '$lib/i18n.svelte';
	import ArrowUpRightIcon from '@lucide/svelte/icons/arrow-up-right';
	import CheckIcon from '@lucide/svelte/icons/check';
	import GitForkIcon from '@lucide/svelte/icons/git-fork';
	import ImageIcon from '@lucide/svelte/icons/image';
	import MaximizeIcon from '@lucide/svelte/icons/maximize-2';
	import PencilIcon from '@lucide/svelte/icons/pencil';
	import WandIcon from '@lucide/svelte/icons/wand-sparkles';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const forkUrl = `${repoUrl}/fork`;
	const mosaic = collageImages.slice(0, 6);
	// A painted generation stands in for Railway's illustrated dusk sky.
	const backdrop = collageLandingSources(collageImage('mountain_chibbi.png'));
	const board = [
		{ id: 'live', icon: PencilIcon },
		{ id: 'gen', icon: WandIcon },
		{ id: 'up', icon: MaximizeIcon },
		{ id: 'edit', icon: ImageIcon }
	] as const;
	const bands = [
		{ label: 'caps.kicker', title: 'caps.gen_title', body: 'caps.gen_body', visual: 'mosaic' },
		{ label: 'nav.features', title: 'features.f3_title', body: 'features.f3_body', visual: 'shot' },
		{
			label: 'caps.live_title',
			title: 'features.f1_title',
			body: 'caps.live_body',
			visual: 'field'
		}
	] as const;
	const forkPoints = ['b1', 'b2', 'b3'] as const;
	const bullets = ['b1', 'b2', 'b3'] as const;
	const tiers = [
		{ key: 't1', price: '9', featured: false },
		{ key: 't2', price: '24', featured: true },
		{ key: 't3', price: '59', featured: false },
		{ key: 't4', price: null, featured: false }
	] as const;
</script>

<div class="krea depot">
	<header>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<nav aria-label={t('nav.features')}>
			<a href="#bands">{t('nav.features')}</a>
			<a href="#models">{t('bench.col_model')}</a>
			<a href="#pricing">{t('nav.pricing')}</a>
			<a href="#run">{t('nav.open')}</a>
			<a href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
		</nav>
		<div class="head-actions">
			<a class="pill pill-ghost" href={repoUrl}>{t('fork.cta_source')}</a>
			<a class="pill pill-accent" href={resolve('/app')}>{t('nav.launch')}</a>
		</div>
	</header>

	<main>
		<!-- Every section is an inset rounded panel, the way railway.com stacks them. -->
		<section class="slab sky">
			<img class="backdrop" src={backdrop.src} srcset={backdrop.srcset} alt="" aria-hidden="true" />
			<div class="dusk" aria-hidden="true"></div>

			<div class="sky-copy">
				<h1>{t('hero.title1')} {t('hero.title2')}</h1>
				<p>
					{t('hero.sub')}
				</p>
				<div class="actions">
					<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
					<a class="pill pill-ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
				</div>
			</div>

			<div class="board">
				<div class="board-head">
					<span class="crumb">potocolom / {t('app.title')}</span>
					<span class="dot" aria-hidden="true"></span>
				</div>
				<div class="board-canvas"><HeroImageField /></div>
				<div class="board-bar">
					{#each board as item (item.id)}
						<a href="#bands">
							<item.icon aria-hidden="true" />
							{t(`caps.${item.id}_title`)}
						</a>
					{/each}
				</div>
			</div>
		</section>

		<section id="models" class="slab wall" aria-label={t('bench.specs')}>
			{#each MODEL_SPECS.slice(0, 9) as spec (spec.id)}
				<div>
					<strong>{spec.name}</strong>
					<span>{spec.architecture}</span>
				</div>
			{/each}
		</section>

		<section id="bands" class="bands">
			{#each bands as band, index (band.title)}
				<article class="slab" class:flip={index % 2 === 1}>
					<div class="band-copy">
						<span class="tag">{t(band.label)}</span>
						<h2>{t(band.title)}</h2>
						<p>{t(band.body)}</p>
						<a class="text-link" href={resolve('/app')}>
							{t('hero.cta_launch')}
							<ArrowUpRightIcon aria-hidden="true" />
						</a>
					</div>
					<div class="band-visual">
						{#if band.visual === 'mosaic'}
							<div class="mosaic">
								{#each mosaic as tile (tile.file)}
									{@const sources = collageLandingSources(tile)}
									<img src={sources.src} srcset={sources.srcset} alt={tile.alt} loading="lazy" />
								{/each}
							</div>
						{:else if band.visual === 'shot'}
							<img class="shot" src="/og.png" alt={t('app.title')} loading="lazy" />
						{:else}
							<div class="field"><HeroImageField /></div>
						{/if}
					</div>
				</article>
			{/each}
		</section>

		<section id="pricing" class="slab pricing">
			<div class="slab-head">
				<span class="tag">{t('pricing.kicker')}</span>
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

		<section id="run" class="slab run">
			<div class="slab-head">
				<span class="tag">{t('nav.open')}</span>
				<h2>{t('fork.title')}</h2>
			</div>
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
				<ForkTerminal class="depot-terminal" />
			</div>
		</section>

		<section class="slab closing">
			<h2>{t('wl.title')}</h2>
			<p>{t('wl.sub')}</p>
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/app')}>{t('wl.cta')}</a>
				<a class="pill pill-ghost" href={repoUrl}>{t('fork.cta_source')}</a>
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
	/* Hallmark - macrostructure: Stacked Slabs - genre: cinematic product - studied DNA: railway.com (inset rounded panels, serif display, muted violet, painted sky, product board with a labelled toolbar) - enrichment: our own generation as the sky, the live field as the board, a model wall in place of a logo wall - contrast: pass - mobile: pass */
	.depot {
		min-width: 0;
		overflow-x: clip;
		background: var(--k-paper);
	}

	header {
		position: relative;
		z-index: 3;
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 1rem;
		padding: 1rem clamp(1rem, 3vw, 2rem);
	}

	.mark {
		font-size: 1.05rem;
		font-weight: 700;
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

	.head-actions {
		display: flex;
		align-items: center;
		gap: 0.6rem;
	}

	main {
		display: grid;
		gap: clamp(0.75rem, 1.5vw, 1.25rem);
		padding-inline: clamp(0.75rem, 1.5vw, 1.25rem);
	}

	/* Slabs ------------------------------------------------------------------ */
	.slab {
		position: relative;
		overflow: clip;
		border: 1px solid var(--k-line);
		border-radius: clamp(1rem, 2vw, 1.75rem);
		background: var(--k-band);
	}

	.slab-head {
		display: grid;
		justify-items: start;
		gap: 0.85rem;
		max-width: 54ch;
	}

	h1,
	h2 {
		font-family: var(--k-serif);
		font-weight: 500;
		letter-spacing: -0.02em;
	}

	h1 {
		max-width: 16ch;
		font-size: clamp(2.6rem, 5.5vw, 4.6rem);
		line-height: 1.04;
	}

	h2 {
		max-width: 20ch;
		font-size: clamp(1.9rem, 3.4vw, 3rem);
		line-height: 1.06;
	}

	.tag {
		padding: 0.3rem 0.7rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: var(--k-panel);
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.72rem;
	}

	.text-link {
		display: inline-flex;
		align-items: center;
		color: var(--k-ink);
		font-weight: 600;
		text-decoration: underline;
		text-underline-offset: 0.3em;
	}

	/* Sky -------------------------------------------------------------------- */
	.sky {
		display: grid;
		justify-items: center;
		gap: clamp(2rem, 5vw, 3.5rem);
		padding: clamp(3rem, 8vw, 6rem) clamp(1rem, 3vw, 2.5rem) 0;
	}

	.backdrop,
	.dusk {
		position: absolute;
		inset: 0;
	}

	.backdrop {
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: center 35%;
		filter: brightness(0.5) saturate(0.75);
	}

	.dusk {
		background:
			radial-gradient(70% 45% at 50% 0%, oklch(0.47 0.12 300 / 30%) 0%, transparent 70%),
			linear-gradient(to bottom, transparent 0%, oklch(0.14 0.018 292 / 55%) 45%, var(--k-band) 92%);
	}

	.sky-copy,
	.board {
		position: relative;
		z-index: 2;
	}

	.sky-copy {
		display: grid;
		justify-items: center;
		gap: 1.1rem;
		max-width: 44rem;
		text-align: center;
	}

	.sky-copy p {
		max-width: 46ch;
		color: var(--k-muted);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 0.7rem;
	}

	/* Board ------------------------------------------------------------------ */
	.board {
		width: min(68rem, 100%);
		overflow: clip;
		border: 1px solid var(--k-line);
		border-radius: 0.9rem 0.9rem 0 0;
		background: oklch(0.11 0.014 292);
		box-shadow: 0 2rem 5rem oklch(0 0 0 / 60%);
	}

	.board-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 0.55rem 0.85rem;
		border-block-end: 1px solid var(--k-line);
	}

	.crumb {
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.68rem;
	}

	.dot {
		width: 0.6rem;
		height: 0.6rem;
		border-radius: 999px;
		background: var(--k-accent);
	}

	.board-canvas {
		aspect-ratio: 16 / 8;
		min-height: 13rem;
		background:
			radial-gradient(circle at 1px 1px, oklch(1 0 0 / 7%) 1px, transparent 0) 0 0 / 24px 24px,
			oklch(0.11 0.014 292);
	}

	.board-bar {
		display: flex;
		overflow-x: auto;
		border-block-start: 1px solid var(--k-line);
	}

	.board-bar a {
		display: inline-flex;
		flex: 1;
		align-items: center;
		justify-content: center;
		gap: 0.45rem;
		padding: 0.8rem 1rem;
		border-inline-end: 1px solid var(--k-line);
		color: var(--k-muted);
		font-size: 0.8rem;
	}

	.board-bar a:last-child {
		border-inline-end: 0;
	}

	.board-bar a:hover {
		color: var(--k-ink);
		background: oklch(1 0 0 / 4%);
	}

	.board-bar :global(svg) {
		width: 0.95rem;
		height: 0.95rem;
	}

	/* Model wall -------------------------------------------------------------- */
	.wall {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 1px;
		background: var(--k-line);
	}

	.wall div {
		display: grid;
		gap: 0.2rem;
		padding: 1.4rem 1.25rem;
		background: var(--k-band);
		text-align: center;
	}

	.wall strong {
		font-size: 0.95rem;
		font-weight: 600;
	}

	.wall span {
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.72rem;
	}

	/* Bands -------------------------------------------------------------------- */
	.bands {
		display: grid;
		gap: clamp(0.75rem, 1.5vw, 1.25rem);
	}

	.bands article {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: clamp(1.5rem, 4vw, 3.5rem);
		align-items: center;
		padding: clamp(1.75rem, 4vw, 3.5rem);
	}

	.band-copy {
		display: grid;
		justify-items: start;
		gap: 0.9rem;
		min-width: 0;
	}

	.band-copy p {
		max-width: 44ch;
		color: var(--k-muted);
	}

	.band-visual {
		min-width: 0;
	}

	.mosaic {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 0.5rem;
	}

	.mosaic img {
		width: 100%;
		aspect-ratio: 1;
		border-radius: 0.6rem;
		object-fit: cover;
	}

	.shot,
	.field {
		width: 100%;
		border: 1px solid var(--k-line);
		border-radius: 0.75rem;
	}

	.field {
		aspect-ratio: 16 / 10;
		overflow: clip;
		background: oklch(0.11 0.014 292);
	}

	/* Pricing ------------------------------------------------------------------- */
	.pricing,
	.run,
	.closing {
		display: grid;
		gap: clamp(1.5rem, 3vw, 2.5rem);
		padding: clamp(2rem, 5vw, 4rem);
	}

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
		border-radius: 1rem;
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
		font-family: var(--k-mono);
		font-size: 0.78rem;
	}

	.tier-badge {
		padding: 0.2rem 0.6rem;
		border-radius: 999px;
		background: var(--k-accent);
		color: var(--k-accent-ink);
		font-size: 0.7rem;
		white-space: nowrap;
	}

	.tier-price {
		display: flex;
		align-items: baseline;
		gap: 0.45rem;
	}

	.amount {
		font-family: var(--k-serif);
		font-size: clamp(1.9rem, 2.4vw, 2.4rem);
		font-weight: 500;
		font-variant-numeric: tabular-nums;
	}

	.amount-word {
		font-size: clamp(1.4rem, 1.9vw, 1.8rem);
	}

	.term {
		color: var(--k-muted);
		font-size: 0.88rem;
	}

	.trial,
	.pricing .slab-head p {
		max-width: 60ch;
		color: var(--k-muted);
		font-size: 0.88rem;
	}

	/* Run ---------------------------------------------------------------------- */
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
		border-radius: 1rem;
		background: var(--k-panel);
	}

	.run .actions {
		justify-content: flex-start;
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
		font-size: 0.9rem;
	}

	.tiers li :global(svg),
	.run-card li :global(svg),
	.pill :global(svg),
	.text-link :global(svg) {
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

	.pill-ghost :global(svg),
	.text-link :global(svg) {
		margin-inline: 0.4rem 0;
		opacity: 0.7;
	}

	.depot :global(.depot-terminal) {
		min-width: 0;
		border-radius: 1rem;
	}

	/* Closing ------------------------------------------------------------------ */
	.closing {
		justify-items: center;
		gap: 1.1rem;
		padding-block: clamp(3rem, 8vw, 6rem);
		text-align: center;
	}

	.closing h2 {
		max-width: 18ch;
	}

	.closing p {
		max-width: 48ch;
		color: var(--k-muted);
	}

	footer {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 1rem 2rem;
		padding: 2.5rem clamp(1.5rem, 3vw, 2.5rem);
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

		.bands article {
			grid-template-columns: minmax(0, 0.85fr) minmax(0, 1.15fr);
		}

		.bands article.flip .band-copy {
			order: 2;
		}

		.run-body {
			grid-template-columns: minmax(0, 0.88fr) minmax(0, 1.12fr);
		}

		.tiers {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}

	@media (min-width: 64rem) {
		.tiers {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}
	}

	@media (max-width: 48rem) {
		.wall {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}

	@media (max-width: 30rem) {
		.head-actions .pill-ghost {
			display: none;
		}

		.actions {
			width: 100%;
			flex-direction: column;
		}

		.actions .pill {
			width: 100%;
		}
	}
</style>
