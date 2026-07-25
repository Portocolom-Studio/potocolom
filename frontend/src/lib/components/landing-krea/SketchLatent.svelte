<script lang="ts">
	import { resolve } from '$app/paths';
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import { collageImages, collageLandingSources } from '$lib/collage-images';
	import { t } from '$lib/i18n.svelte';
	import { onMount } from 'svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const tiles = collageImages.slice(0, 10);

	let index = $state(0);
	const tile = $derived(tiles[index]);
	const sources = $derived(collageLandingSources(tile));

	function step(direction: 1 | -1) {
		index = (index + direction + tiles.length) % tiles.length;
	}

	onMount(() => {
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
		const timer = setInterval(() => step(1), 5000);
		return () => clearInterval(timer);
	});
</script>

<div class="krea latent">
	<div class="canvas"><LatentCanvas followCursor animate warmupFrames={1400} /></div>
	<div class="veil" aria-hidden="true"></div>

	<header>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
	</header>

	<div class="stage">
		<h1>{t('hero.title1')} {t('hero.title2')}</h1>
		<p>{t('hero.sub')}</p>
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
			<span class="label">{t('gallery.kicker')}</span>
			<span class="alt">{tile.alt}</span>
			<span class="steps">
				<button type="button" onclick={() => step(-1)} aria-label={t('nav.scroll_to_top')}>
					&lt;
				</button>
				<span class="count">{index + 1}/{tiles.length}</span>
				<button type="button" onclick={() => step(1)} aria-label={t('gallery.kicker')}>&gt;</button>
			</span>
		</figcaption>
	</figure>
</div>

<style>
	/* Hallmark - macrostructure: Latent Field - genre: abstract atmospheric - enrichment: the latent canvas as the surface, one real generation resolving beside it - contrast: pass - mobile: pass */
	.latent {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		grid-template-rows: auto 1fr auto;
		height: 100svh;
		overflow: clip;
	}

	.canvas,
	.veil {
		position: absolute;
		inset: 0;
	}

	.veil {
		background:
			radial-gradient(38% 46% at 26% 52%, oklch(0.62 0.2 255 / 22%) 0%, transparent 72%),
			radial-gradient(70% 60% at 40% 45%, transparent 0%, var(--k-veil) 88%);
	}

	header,
	.stage,
	.resolved {
		position: relative;
		z-index: 2;
	}

	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 1.1rem clamp(1rem, 3vw, 2.5rem);
	}

	.mark {
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.03em;
	}

	.stage {
		display: grid;
		align-content: center;
		gap: 1.1rem;
		max-width: 44rem;
		padding-inline: clamp(1rem, 5vw, 4rem);
	}

	h1 {
		font-size: clamp(2.7rem, 6.5vw, 5.6rem);
		line-height: 0.95;
	}

	.stage p {
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
		gap: 0.75rem;
		justify-self: end;
		width: min(22rem, calc(100% - 2rem));
		margin: clamp(1rem, 3vw, 2.5rem);
		margin-block-start: 0;
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

	figcaption {
		display: grid;
		gap: 0.35rem;
	}

	.label {
		color: var(--k-accent);
		font-family: var(--k-mono);
		font-size: 0.66rem;
		letter-spacing: 0.09em;
		text-transform: uppercase;
	}

	.alt {
		color: var(--k-ink);
		font-size: 0.92rem;
	}

	.steps {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.72rem;
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
	}

	.count {
		font-variant-numeric: tabular-nums;
	}

	@media (min-width: 48rem) {
		.latent {
			grid-template-rows: auto 1fr;
		}

		.stage {
			align-self: center;
		}

		.resolved {
			position: absolute;
			inset-block-end: 0;
			inset-inline-end: 0;
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
