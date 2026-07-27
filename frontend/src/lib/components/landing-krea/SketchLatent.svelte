<script lang="ts">
	import { resolve } from '$app/paths';
	import ForkTerminal from '$lib/components/ForkTerminal.svelte';
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import PromptMarquee from '$lib/components/PromptMarquee.svelte';
	import { collageImages, collageLandingSources } from '$lib/collage-images';
	import { makingImages, makingSources } from '$lib/making-images';
	import KreaWaitlist from './KreaWaitlist.svelte';
	import SalonGrid, { type SalonTile } from './SalonGrid.svelte';
	import { t } from '$lib/i18n.svelte';
	import { onMount } from 'svelte';
	import ArrowUpRightIcon from '@lucide/svelte/icons/arrow-up-right';
	import CheckIcon from '@lucide/svelte/icons/check';
	import GitForkIcon from '@lucide/svelte/icons/git-fork';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const forkUrl = `${repoUrl}/fork`;
	const resolving = collageImages.slice(0, 10);
	const wall: SalonTile[] = makingImages.map((image) => ({
		key: image.id,
		alt: image.alt,
		...makingSources(image)
	}));
	const capabilities = ['live', 'gen', 'up', 'edit'] as const;
	const forkPoints = ['b1', 'b2', 'b3'] as const;
	const tiers = [
		{ key: 't1', price: '9', featured: false },
		{ key: 't2', price: '24', featured: true },
		{ key: 't3', price: '59', featured: false },
		{ key: 't4', price: null, featured: false }
	] as const;
	const bullets = ['b1', 'b2', 'b3'] as const;

	let index = $state(0);
	const tile = $derived(resolving[index]);
	const sources = $derived(collageLandingSources(tile));

	let shownTile = $state<SalonTile | null>(null);

	function step(direction: 1 | -1) {
		index = (index + direction + resolving.length) % resolving.length;
	}

	onMount(() => {
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
		const timer = setInterval(() => step(1), 5000);
		return () => clearInterval(timer);
	});
</script>

<div class="krea latent">
	<div class="canvas" aria-hidden="true">
		<LatentCanvas followCursor animate warmupFrames={1400} />
	</div>
	<div class="veil" aria-hidden="true"></div>

	<header>
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

	<main>
		<section class="opening">
			<div class="stage">
				<h1>{t('hero.title1')} {t('hero.title2')}</h1>
				<p class="lede">{t('hero.sub')}</p>
				<div class="actions">
					<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
					<a class="pill pill-ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
				</div>
			</div>

			<figure class="resolved">
				{#key tile.file}
					<img src={sources.src} srcset={sources.srcset} alt={tile.alt} />
				{/key}
				<figcaption>
					<span class="piece">{tile.alt}</span>
					<span class="meta">
						<span>{t('gallery.kicker')}</span>
						<span class="steps">
							<button type="button" onclick={() => step(-1)} aria-label={t('nav.scroll_to_top')}>
								&lt;
							</button>
							<span class="count">{String(index + 1).padStart(2, '0')}/{resolving.length}</span>
							<button type="button" onclick={() => step(1)} aria-label={t('gallery.kicker')}>
								&gt;
							</button>
						</span>
					</span>
				</figcaption>
			</figure>
		</section>

		<section id="does" class="panel does">
			<div class="panel-head">
				<h2>{t('caps.title')}</h2>
				<p>{t('caps.sub')}</p>
			</div>
			<div class="does-grid">
				{#each capabilities as capability (capability)}
					<article>
						<h3>{t(`caps.${capability}_title`)}</h3>
						<p>{t(`caps.${capability}_body`)}</p>
					</article>
				{/each}
			</div>
		</section>

		<section id="work" class="work">
			<div class="work-stage">
				<div class="work-wall">
					<SalonGrid tiles={wall} onactive={(next) => (shownTile = next)} />
				</div>
				<div class="work-plate">
					{#if shownTile}
						<span class="piece">{shownTile.alt}</span>
						<span class="meta"><span>{t('gallery.kicker')}</span></span>
					{:else}
						<h2>{t('gallery.title_before')} {t('gallery.word_making')}</h2>
						<p>{t('gallery.sub')}</p>
					{/if}
				</div>
			</div>
			<PromptMarquee />
		</section>

		<section class="panel studio">
			<div class="panel-head">
				<h2>{t('features.f3_title')}</h2>
				<p>{t('features.f3_body')}</p>
			</div>
			<img class="shot" src="/og.png" alt={t('app.title')} loading="lazy" />
		</section>

		<section id="pricing" class="panel pricing">
			<div class="panel-head">
				<h2>{t('pricing.title')}</h2>
				<p>{t('pricing.sub')}</p>
			</div>
			<div class="tiers">
				{#each tiers as tier (tier.key)}
					<article class:featured={tier.featured}>
						<div class="tier-head">
							<span class="tier-name">{t(`pricing.${tier.key}_name`)}</span>
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
							<a class="tier-contact" href="mailto:admin@leonfuller.com">
								{t('footer.contact')}
							</a>
						{/if}
					</article>
				{/each}
			</div>
			<p class="trial">{t('pricing.trial')}</p>
		</section>

		<section id="run" class="panel run">
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
					<div class="run-actions">
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
				<ForkTerminal class="latent-terminal" />
			</div>
		</section>
	</main>

	<KreaWaitlist field={false} />

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
	/* Hallmark - macrostructure: Latent Field - genre: abstract atmospheric - enrichment: the latent canvas stays fixed behind the whole page while panels float over it - contrast: pass - mobile: pass */
	.latent {
		position: relative;
		min-width: 0;
		overflow-x: clip;
	}

	/* Fixed, so the field keeps breathing behind every section as you scroll. */
	.canvas,
	.veil {
		position: fixed;
		inset: 0;
		z-index: 0;
	}

	.veil {
		background:
			radial-gradient(38% 46% at 26% 42%, oklch(0.62 0.2 255 / 20%) 0%, transparent 72%),
			radial-gradient(70% 60% at 40% 40%, transparent 0%, var(--k-veil) 88%);
		pointer-events: none;
	}

	header,
	main,
	footer {
		position: relative;
		z-index: 1;
	}

	header {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 1rem;
		padding: 1.1rem clamp(1rem, 3vw, 2.5rem);
	}

	.mark {
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.03em;
	}

	header nav {
		display: none;
		justify-content: center;
		gap: 1.75rem;
		color: var(--k-muted);
		font-size: 0.9rem;
	}

	header nav a:hover {
		color: var(--k-ink);
	}

	/* Opening -------------------------------------------------------------- */
	.opening {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		align-content: center;
		min-height: calc(100svh - 5rem);
		padding: clamp(2rem, 6vw, 4rem) clamp(1rem, 5vw, 4rem);
	}

	.stage {
		display: grid;
		gap: 1.1rem;
		max-width: 44rem;
	}

	h1 {
		font-size: clamp(2.7rem, 6.5vw, 5.6rem);
		line-height: 0.95;
	}

	.lede {
		max-width: 42ch;
		color: var(--k-muted);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
	}

	.resolved {
		display: grid;
		gap: 0.85rem;
		width: min(22rem, 100%);
		margin: 2.5rem 0 0;
	}

	.resolved img {
		width: 100%;
		aspect-ratio: 1;
		border: 1px solid var(--k-line);
		border-radius: 1rem;
		object-fit: cover;
		animation: settle 700ms var(--k-ease);
	}

	@keyframes settle {
		from {
			opacity: 0;
			filter: blur(18px);
			transform: scale(1.02);
		}
	}

	figcaption,
	.work-plate {
		display: grid;
		gap: 0.45rem;
	}

	/* Caption type: sentence case, no mono, no letter-spaced uppercase label. */
	.piece {
		padding-block-start: 0.65rem;
		border-block-start: 1px solid var(--k-line);
		color: var(--k-ink);
		font-size: 1.05rem;
		font-weight: 600;
		letter-spacing: -0.02em;
	}

	.meta {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		color: var(--k-muted);
		font-size: 0.85rem;
	}

	.steps {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.steps button {
		width: 1.7rem;
		height: 1.7rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: none;
		color: inherit;
		cursor: pointer;
		font: inherit;
		line-height: 1;
	}

	.count {
		font-variant-numeric: tabular-nums;
	}

	/* Panels --------------------------------------------------------------- */
	.panel {
		display: grid;
		gap: clamp(1.75rem, 4vw, 3rem);
		padding: clamp(3rem, 8vw, 6rem) clamp(1rem, 4vw, 3rem);
	}

	/* These two read as cards in a row, so they hold to the main landing's
	   72rem measure instead of stretching the full width. */
	.pricing,
	.run {
		padding-inline: max(clamp(1rem, 4vw, 3rem), calc((100% - 72rem) / 2));
	}

	.panel {
		border-block-start: 1px solid var(--k-line);
		background: oklch(0.08 0.012 265 / 72%);
		backdrop-filter: blur(28px);
	}

	:global(:root[data-krea-mode='light']) .panel {
		background: oklch(0.97 0.004 255 / 78%);
	}

	.panel-head {
		display: grid;
		gap: 0.9rem;
		max-width: 52ch;
	}

	h2 {
		font-size: clamp(1.9rem, 4vw, 3.2rem);
		line-height: 1.02;
	}

	h3 {
		font-size: 1.15rem;
		font-weight: 700;
	}

	.panel-head p,
	.does-grid p,
	.work-plate p {
		color: var(--k-muted);
	}

	.does-grid {
		display: grid;
		gap: 1.75rem;
	}

	.does-grid article {
		display: grid;
		gap: 0.45rem;
		padding-block-start: 0.9rem;
		border-block-start: 1px solid var(--k-line);
	}

	.does-grid p {
		max-width: 34ch;
		font-size: 0.92rem;
	}

	/* Work wall ------------------------------------------------------------ */
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
	}

	.work-wall {
		position: relative;
		z-index: 0;
		width: 100%;
		height: auto;
		overflow: visible;
		/* Opaque: dimmed tiles must fade to paper, never onto the moving canvas. */
		background: var(--k-paper);
	}

	.work-plate {
		position: absolute;
		z-index: 2;
		inset-block-end: clamp(1rem, 3vw, 2.5rem);
		inset-inline-start: clamp(1rem, 3vw, 2.5rem);
		display: grid;
		gap: 0.45rem;
		width: min(38rem, calc(100% - 2rem));
		padding: clamp(1.25rem, 3vw, 2rem);
		border: 1px solid var(--k-line);
		border-radius: 1.25rem;
		background: var(--k-panel);
		backdrop-filter: blur(24px);
	}

	/* Studio --------------------------------------------------------------- */
	.shot {
		width: 100%;
		border: 1px solid var(--k-line);
		border-radius: 1rem;
	}

	/* Pricing --------------------------------------------------------------- */
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

	.term {
		color: var(--k-muted);
		font-size: 0.9rem;
	}

	.amount-word {
		font-size: clamp(1.5rem, 2vw, 1.9rem);
	}

	/* The enterprise card carries a mail link instead of a price. */
	.tier-contact {
		justify-self: start;
		color: var(--k-accent);
		font-weight: 700;
		text-decoration: underline;
		text-underline-offset: 0.25em;
	}

	.trial {
		max-width: 60ch;
		color: var(--k-muted);
		font-size: 0.88rem;
	}

	/* Run ------------------------------------------------------------------ */
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

	.run-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
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

	.latent :global(.latent-terminal) {
		min-width: 0;
		border-radius: 1rem;
	}

	footer {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 1rem 2rem;
		padding: 2rem clamp(1rem, 4vw, 3rem);
		border-block-start: 1px solid var(--k-line);
		background: oklch(0.08 0.012 265 / 72%);
		color: var(--k-muted);
		font-size: 0.85rem;
		backdrop-filter: blur(28px);
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

		.opening {
			grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.85fr);
			align-items: center;
			gap: clamp(2rem, 6vw, 5rem);
		}

		.resolved {
			justify-self: end;
			margin: 0;
		}

		.does-grid {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}

		.studio {
			grid-template-columns: minmax(0, 0.85fr) minmax(0, 1.15fr);
			align-items: center;
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
		.work-stage {
			min-height: 0;
		}

		.work-plate {
			position: relative;
			inset: auto;
			margin: -2.5rem clamp(1rem, 3vw, 2.5rem) clamp(1rem, 3vw, 2.5rem);
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

		.resolved img {
			aspect-ratio: 16 / 9;
		}
	}
</style>
