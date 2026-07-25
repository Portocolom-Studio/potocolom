<script lang="ts">
	import { resolve } from '$app/paths';
	import ForkTerminal from '$lib/components/ForkTerminal.svelte';
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import { collageImages, collageLandingSources, type CollageImage } from '$lib/collage-images';
	import SalonGrid from './SalonGrid.svelte';
	import { t } from '$lib/i18n.svelte';
	import { onMount } from 'svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const resolving = collageImages.slice(0, 10);
	const wall = collageImages.slice(0, 18);
	const capabilities = ['live', 'gen', 'up', 'edit'] as const;
	const forkPoints = ['b1', 'b2', 'b3'] as const;

	let index = $state(0);
	const tile = $derived(resolving[index]);
	const sources = $derived(collageLandingSources(tile));

	let shownTile = $state<CollageImage | null>(null);

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
			<a href="#run">{t('nav.open')}</a>
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
			<div class="work-wall">
				<SalonGrid tiles={wall} columns={6} rows="26svh" onactive={(next) => (shownTile = next)} />
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
		</section>

		<section class="panel studio">
			<div class="panel-head">
				<h2>{t('features.f3_title')}</h2>
				<p>{t('features.f3_body')}</p>
			</div>
			<img class="shot" src="/og.png" alt={t('app.title')} loading="lazy" />
		</section>

		<section id="run" class="panel run">
			<div class="run-copy">
				<h2>{t('fork.title')}</h2>
				<ul>
					{#each forkPoints as point (point)}
						<li>{t(`fork.${point}`)}</li>
					{/each}
				</ul>
				<a class="pill pill-ghost" href={repoUrl}>{t('fork.cta_source')}</a>
			</div>
			<ForkTerminal class="latent-terminal" />
		</section>

		<section class="closing">
			<h2>{t('wl.title')}</h2>
			<p>{t('wl.sub')}</p>
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
				<a class="pill pill-ghost" href={repoUrl}>{t('fork.cta_fork')}</a>
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
	.panel,
	.closing {
		display: grid;
		gap: clamp(1.75rem, 4vw, 3rem);
		padding: clamp(3rem, 8vw, 6rem) clamp(1rem, 4vw, 3rem);
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
	.run-copy li,
	.closing p,
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
		display: grid;
		align-content: end;
		min-height: 78svh;
	}

	.work-wall {
		position: absolute;
		inset: 0;
	}

	.work-plate {
		position: relative;
		z-index: 2;
		justify-self: start;
		width: min(38rem, calc(100% - 2rem));
		margin: clamp(1rem, 3vw, 2.5rem);
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

	/* Run ------------------------------------------------------------------ */
	.run-copy {
		display: grid;
		justify-items: start;
		gap: 1.25rem;
	}

	.run-copy ul {
		display: grid;
		gap: 0.8rem;
		max-width: 44ch;
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.run-copy li {
		padding-inline-start: 1rem;
		border-inline-start: 2px solid var(--k-accent);
		line-height: 1.6;
	}

	.latent :global(.latent-terminal) {
		min-width: 0;
		border-radius: 1rem;
	}

	/* Closing -------------------------------------------------------------- */
	.closing {
		justify-items: center;
		gap: 1.1rem;
		text-align: center;
	}

	.closing p {
		max-width: 48ch;
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

		.studio,
		.run {
			grid-template-columns: minmax(0, 0.85fr) minmax(0, 1.15fr);
			align-items: center;
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
