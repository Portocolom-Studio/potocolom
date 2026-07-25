<script lang="ts">
	import { resolve } from '$app/paths';
	import ForkTerminal from '$lib/components/ForkTerminal.svelte';
	import HeroImageField from '$lib/components/HeroImageField.svelte';
	import { collageImages, type CollageImage } from '$lib/collage-images';
	import SalonGrid from './SalonGrid.svelte';
	import { t } from '$lib/i18n.svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const mosaic = collageImages.slice(0, 18);

	let shownTile = $state<CollageImage | null>(null);
	const acts = [
		{ id: 'draw', label: 'caps.live_title' },
		{ id: 'work', label: 'gallery.kicker' },
		{ id: 'studio', label: 'app.title' },
		{ id: 'yours', label: 'nav.open' },
		{ id: 'start', label: 'nav.launch' }
	] as const;
</script>

<div class="krea reel">
	<a class="mark" href={resolve('/')}>potocolom</a>

	<nav class="dots" aria-label={t('wp.toc')}>
		{#each acts as act (act.id)}
			<a href="#act-{act.id}"><span></span>{t(act.label)}</a>
		{/each}
	</nav>

	<section id="act-draw" class="act">
		<div class="media"><HeroImageField /></div>
		<div class="veil" aria-hidden="true"></div>
		<div class="copy center">
			<h1>{t('hero.title1')} {t('hero.title2')}</h1>
			<p>{t('hero.sub')}</p>
			<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
		</div>
	</section>

	<section id="act-work" class="act">
		<div class="media">
			<SalonGrid
				tiles={mosaic}
				columns={6}
				rows="33.34svh"
				onactive={(tile) => (shownTile = tile)}
			/>
		</div>
		<div class="veil" class:lifted={shownTile !== null} aria-hidden="true"></div>
		<div class="copy bottom">
			<div class="plate">
				{#if shownTile}
					<p class="tag">{t('gallery.kicker')}</p>
					<h2>{shownTile.alt}</h2>
				{:else}
					<h2>{t('gallery.title_before')} {t('gallery.word_making')}</h2>
					<p>{t('gallery.sub')}</p>
				{/if}
			</div>
		</div>
	</section>

	<section id="act-studio" class="act">
		<img class="media shot" src="/og.png" alt={t('app.title')} loading="lazy" />
		<div class="veil" aria-hidden="true"></div>
		<div class="copy bottom">
			<h2>{t('caps.gen_title')}, {t('caps.up_title')}, {t('caps.edit_title')}</h2>
			<p>{t('caps.sub')}</p>
		</div>
	</section>

	<section id="act-yours" class="act plain">
		<div class="split">
			<div class="copy">
				<h2>{t('fork.title')}</h2>
				<p>{t('fork.b3')}</p>
				<a class="pill pill-ghost" href={repoUrl}>{t('fork.cta_source')}</a>
			</div>
			<ForkTerminal class="reel-terminal" />
		</div>
	</section>

	<section id="act-start" class="act plain">
		<div class="copy center">
			<h2>{t('wl.title')}</h2>
			<p>{t('wl.sub')}</p>
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
				<a class="pill pill-ghost" href={repoUrl}>{t('fork.cta_fork')}</a>
			</div>
			<p class="fine">{t('footer.tagline')}</p>
		</div>
	</section>
</div>

<style>
	/* Hallmark - macrostructure: Snap Reel - genre: cinematic sequence - enrichment: five full-viewport acts, one line of copy each - contrast: pass - mobile: pass */
	.reel {
		height: 100svh;
		overflow-y: auto;
		overflow-x: clip;
		scroll-snap-type: y mandatory;
		scroll-behavior: smooth;
	}

	.mark {
		position: fixed;
		inset-block-start: 1.1rem;
		inset-inline-start: clamp(1rem, 3vw, 2.5rem);
		z-index: 5;
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.03em;
		mix-blend-mode: difference;
	}

	.dots {
		position: fixed;
		inset-block-start: 50%;
		inset-inline-end: clamp(0.75rem, 2vw, 1.75rem);
		z-index: 5;
		display: none;
		flex-direction: column;
		align-items: flex-end;
		gap: 0.85rem;
		transform: translateY(-50%);
	}

	.dots a {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.68rem;
		opacity: 0.55;
		transition: opacity 200ms var(--k-ease);
	}

	.dots a span {
		display: block;
		width: 1.4rem;
		height: 1px;
		background: currentColor;
	}

	.dots a:hover {
		color: var(--k-ink);
		opacity: 1;
	}

	.act {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		height: 100svh;
		overflow: clip;
		scroll-snap-align: start;
		scroll-snap-stop: always;
	}

	.media,
	.veil {
		position: absolute;
		inset: 0;
	}

	.shot {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.veil {
		transition: opacity 320ms var(--k-ease);
		background: linear-gradient(
			to bottom,
			var(--k-veil) 0%,
			transparent 30%,
			transparent 45%,
			var(--k-paper) 100%
		);
	}

	/* The salon act owns its own pointer events; the veil must not swallow them. */
	#act-work .veil {
		pointer-events: none;
	}

	#act-work .veil.lifted {
		opacity: 0.35;
	}

	.tag {
		color: var(--k-accent);
		font-family: var(--k-mono);
		font-size: 0.7rem;
		letter-spacing: 0.09em;
		text-transform: uppercase;
	}

	.copy {
		position: relative;
		z-index: 2;
		display: grid;
		justify-items: start;
		align-content: end;
		gap: 1rem;
		max-width: 46rem;
		padding: clamp(1.5rem, 5vw, 4rem);
	}

	#act-work .copy {
		pointer-events: none;
	}

	.plate {
		display: grid;
		gap: 0.6rem;
		width: min(38rem, 100%);
		padding: clamp(1.25rem, 3vw, 2rem);
		border: 1px solid var(--k-line);
		border-radius: 1.25rem;
		background: var(--k-panel);
		backdrop-filter: blur(24px);
		pointer-events: auto;
	}

	.copy.center {
		align-content: center;
		justify-items: center;
		justify-self: center;
		text-align: center;
	}

	.plain {
		background: var(--k-paper);
		place-content: center;
	}

	h1 {
		font-size: clamp(2.8rem, 7vw, 6rem);
		line-height: 0.95;
	}

	h2 {
		font-size: clamp(2rem, 4.5vw, 3.6rem);
		line-height: 1;
	}

	.copy p {
		max-width: 48ch;
		color: var(--k-muted);
	}

	.fine {
		font-size: 0.82rem;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 0.7rem;
	}

	.split {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: clamp(1.5rem, 4vw, 3rem);
		align-items: center;
		width: min(82rem, 100%);
		margin-inline: auto;
		padding: clamp(1.5rem, 5vw, 4rem);
	}

	.reel :global(.reel-terminal) {
		min-width: 0;
		border-radius: 1rem;
	}

	@media (min-width: 48rem) {
		.dots {
			display: flex;
		}

		.split {
			grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
		}
	}

	@media (max-width: 40rem) {
		/* Three columns at one act's height: anything past nine tiles would be cut. */
		#act-work :global(.salon-grid button:nth-child(n + 10)) {
			display: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.reel {
			scroll-behavior: auto;
		}
	}
</style>
