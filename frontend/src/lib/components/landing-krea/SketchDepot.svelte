<script lang="ts">
	import { resolve } from '$app/paths';
	import ForkTerminal from '$lib/components/ForkTerminal.svelte';
	import HeroImageField from '$lib/components/HeroImageField.svelte';
	import { collageImage, collageImages, collageLandingSources } from '$lib/collage-images';
	import { MODEL_SPECS } from '$lib/model-specs';
	import { t } from '$lib/i18n.svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const capabilities = ['live', 'gen', 'up', 'edit'] as const;
	const mosaic = collageImages.slice(0, 6);
	// A real generation stands in for Railway's painted sky.
	const backdrop = collageLandingSources(collageImage('mountian.jpg'));
	const bands = [
		{ label: 'caps.kicker', title: 'caps.gen_title', body: 'caps.gen_body', visual: 'mosaic' },
		{ label: 'nav.features', title: 'features.f3_title', body: 'features.f3_body', visual: 'shot' },
		{ label: 'nav.open', title: 'fork.title', body: 'fork.b3', visual: 'terminal' }
	] as const;
</script>

<div class="krea depot">
	<header>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<nav aria-label={t('nav.features')}>
			<a href="#bands">{t('nav.features')}</a>
			<a href="#models">{t('bench.col_model')}</a>
			<a href={resolve('/benchmark')}>{t('nav.benchmark')}</a>
		</nav>
		<div class="head-actions">
			<a class="pill pill-ghost" href={repoUrl}>{t('fork.cta_source')}</a>
			<a class="pill pill-accent" href={resolve('/app')}>{t('nav.launch')}</a>
		</div>
	</header>

	<section class="sky">
		<img class="backdrop" src={backdrop.src} srcset={backdrop.srcset} alt="" aria-hidden="true" />
		<div class="dusk" aria-hidden="true"></div>

		<div class="sky-copy">
			<h1>{t('hero.title1')} {t('hero.title2')}</h1>
			<p>{t('hero.sub')}</p>
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
				<a class="pill pill-ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
			</div>
		</div>

		<div class="board">
			<div class="board-canvas"><HeroImageField /></div>
			<div class="board-bar">
				{#each capabilities as capability (capability)}
					<a href="#bands">{t(`caps.${capability}_title`)}</a>
				{/each}
			</div>
		</div>
	</section>

	<section id="models" class="model-wall" aria-label={t('bench.specs')}>
		{#each MODEL_SPECS.slice(0, 9) as spec (spec.id)}
			<div>
				<strong>{spec.name}</strong>
				<span>{spec.architecture}</span>
			</div>
		{/each}
	</section>

	<section id="bands" class="bands">
		{#each bands as band, index (band.title)}
			<article class:flip={index % 2 === 1}>
				<div class="band-copy">
					<span class="tag">{t(band.label)}</span>
					<h2>{t(band.title)}</h2>
					<p>{t(band.body)}</p>
					<a class="text-link" href={index === 2 ? repoUrl : resolve('/app')}>
						{index === 2 ? t('fork.cta_fork') : t('hero.cta_launch')}
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
						<ForkTerminal class="depot-terminal" />
					{/if}
				</div>
			</article>
		{/each}
	</section>

	<section class="closing">
		<h2>{t('wl.title')}</h2>
		<p>{t('wl.sub')}</p>
		<div class="actions">
			<a class="pill pill-accent" href={resolve('/app')}>{t('wl.cta')}</a>
			<a class="pill pill-ghost" href={repoUrl}>{t('fork.cta_source')}</a>
		</div>
	</section>

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
	/* Hallmark - macrostructure: Sky and Board - genre: cinematic product - studied DNA: railway.com - enrichment: a real generation as the painted sky, the live field as the board, model wall instead of a customer logo wall - contrast: pass - mobile: pass */
	.depot {
		min-width: 0;
		overflow-x: clip;
	}

	header {
		position: relative;
		z-index: 3;
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 1rem;
		max-width: 84rem;
		margin-inline: auto;
		padding: 1rem clamp(1rem, 3vw, 2.5rem);
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

	.head-actions {
		display: flex;
		align-items: center;
		gap: 0.6rem;
	}

	/* Sky ------------------------------------------------------------------ */
	.sky {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		justify-items: center;
		gap: clamp(2rem, 5vw, 3.5rem);
		padding: clamp(1.5rem, 5vw, 4rem) clamp(1rem, 4vw, 2.5rem) clamp(3rem, 8vw, 6rem);
		margin-block-start: -5rem;
		padding-block-start: 7rem;
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
		filter: brightness(0.42) saturate(0.85);
	}

	.dusk {
		background:
			radial-gradient(60% 40% at 50% 8%, oklch(0.62 0.2 255 / 22%) 0%, transparent 70%),
			linear-gradient(
				to bottom,
				oklch(0.08 0.012 265 / 55%) 0%,
				transparent 30%,
				var(--k-paper) 96%
			);
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
		max-width: 46rem;
		text-align: center;
	}

	h1 {
		font-size: clamp(2.6rem, 6vw, 5.2rem);
		line-height: 0.98;
	}

	.sky-copy p {
		max-width: 44ch;
		color: var(--k-muted);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 0.7rem;
	}

	/* Board ---------------------------------------------------------------- */
	.board {
		width: min(72rem, 100%);
		overflow: clip;
		border: 1px solid var(--k-screen-line);
		border-radius: 1.5rem;
		background: var(--k-screen);
		box-shadow: 0 2rem 5rem oklch(0 0 0 / 55%);
	}

	.board-canvas {
		aspect-ratio: 16 / 9;
		min-height: 14rem;
		background:
			radial-gradient(circle at 1px 1px, oklch(1 0 0 / 8%) 1px, transparent 0) 0 0 / 22px 22px,
			var(--k-screen);
	}

	.board-bar {
		display: flex;
		overflow-x: auto;
		border-block-start: 1px solid var(--k-screen-line);
	}

	.board-bar a {
		flex: 1;
		padding: 0.85rem 1rem;
		border-inline-end: 1px solid var(--k-screen-line);
		color: color-mix(in oklab, var(--k-screen-ink) 68%, transparent);
		font-size: 0.82rem;
		text-align: center;
	}

	.board-bar a:last-child {
		border-inline-end: 0;
	}

	.board-bar a:hover {
		color: var(--k-screen-ink);
		background: oklch(1 0 0 / 5%);
	}

	/* Model wall ------------------------------------------------------------ */
	.model-wall {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 1px;
		max-width: 84rem;
		margin-inline: auto;
		border: 1px solid var(--k-line);
		border-radius: 1rem;
		overflow: clip;
		background: var(--k-line);
	}

	.model-wall div {
		display: grid;
		gap: 0.2rem;
		padding: 1.1rem 1.25rem;
		background: var(--k-paper);
		text-align: center;
	}

	.model-wall strong {
		font-size: 0.95rem;
	}

	.model-wall span {
		color: var(--k-muted);
		font-size: 0.76rem;
	}

	/* Bands ----------------------------------------------------------------- */
	.bands {
		display: grid;
		gap: clamp(3rem, 8vw, 6rem);
		max-width: 84rem;
		margin-inline: auto;
		padding: clamp(3rem, 8vw, 6rem) clamp(1rem, 4vw, 2.5rem);
	}

	.bands article {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: clamp(1.5rem, 4vw, 3.5rem);
		align-items: center;
	}

	.band-copy {
		display: grid;
		justify-items: start;
		gap: 0.9rem;
		min-width: 0;
	}

	.tag {
		padding: 0.3rem 0.75rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: var(--k-panel);
		color: var(--k-muted);
		font-size: 0.76rem;
	}

	h2 {
		max-width: 18ch;
		font-size: clamp(1.9rem, 3.8vw, 3.2rem);
		line-height: 1.02;
	}

	.band-copy p {
		max-width: 46ch;
		color: var(--k-muted);
	}

	.text-link {
		color: var(--k-accent);
		font-weight: 700;
		text-decoration: underline;
		text-underline-offset: 0.25em;
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
		border-radius: 0.75rem;
		object-fit: cover;
	}

	.shot {
		width: 100%;
		border: 1px solid var(--k-line);
		border-radius: 1rem;
	}

	.depot :global(.depot-terminal) {
		min-width: 0;
		border-radius: 1rem;
	}

	/* Closing --------------------------------------------------------------- */
	.closing {
		display: grid;
		justify-items: center;
		gap: 1.1rem;
		padding: clamp(3rem, 9vw, 7rem) clamp(1rem, 4vw, 2.5rem);
		border-block-start: 1px solid var(--k-line);
		text-align: center;
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
		max-width: 84rem;
		margin-inline: auto;
		padding: 2rem clamp(1rem, 4vw, 2.5rem);
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

		.bands article {
			grid-template-columns: minmax(0, 0.85fr) minmax(0, 1.15fr);
		}

		.bands article.flip .band-copy {
			order: 2;
		}
	}

	@media (max-width: 48rem) {
		.model-wall {
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
