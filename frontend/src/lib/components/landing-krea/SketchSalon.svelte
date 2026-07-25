<script lang="ts">
	import { resolve } from '$app/paths';
	import { collageImages, type CollageImage } from '$lib/collage-images';
	import SalonGrid from './SalonGrid.svelte';
	import { t } from '$lib/i18n.svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const tiles = collageImages.slice(0, 24);

	let shownTile = $state<CollageImage | null>(null);
</script>

<div class="krea salon">
	<SalonGrid {tiles} columns={6} onactive={(tile) => (shownTile = tile)} />

	<header>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
	</header>

	<div class="plate">
		{#if shownTile}
			<p class="label">{t('gallery.kicker')}</p>
			<h2>{shownTile.alt}</h2>
			<p class="hint">{t('gallery.sub')}</p>
		{:else}
			<h1>{t('hero.title1')} {t('hero.title2')}</h1>
			<p class="hint">{t('hero.sub')}</p>
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
				<a class="pill pill-ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
			</div>
		{/if}
	</div>
</div>

<style>
	/* Hallmark - macrostructure: Salon Wall - genre: image-first - enrichment: real generations as the entire surface, copy recedes on focus - contrast: pass - mobile: pass */
	.salon {
		position: relative;
		height: 100svh;
		overflow: clip;
	}

	header,
	.plate {
		position: absolute;
		z-index: 3;
	}

	header {
		inset-block-start: 0;
		inset-inline: 0;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 1rem clamp(1rem, 3vw, 2rem);
		background: linear-gradient(to bottom, var(--k-veil), transparent);
	}

	.mark {
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.03em;
	}

	.plate {
		inset-block-end: clamp(1rem, 3vw, 2.5rem);
		inset-inline-start: clamp(1rem, 3vw, 2.5rem);
		display: grid;
		gap: 0.9rem;
		width: min(38rem, calc(100% - 2rem));
		padding: clamp(1.25rem, 3vw, 2rem);
		border: 1px solid var(--k-line);
		border-radius: 1.25rem;
		background: var(--k-panel);
		backdrop-filter: blur(24px);
	}

	h1 {
		font-size: clamp(2.2rem, 4.5vw, 3.6rem);
		line-height: 0.98;
	}

	h2 {
		font-size: clamp(1.6rem, 3vw, 2.6rem);
		line-height: 1.02;
	}

	.label {
		color: var(--k-accent);
		font-family: var(--k-mono);
		font-size: 0.7rem;
		letter-spacing: 0.09em;
		text-transform: uppercase;
	}

	.hint {
		max-width: 46ch;
		color: var(--k-muted);
		font-size: 0.9rem;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
	}

	@media (max-width: 40rem) {
		.plate {
			inset-inline: 0.75rem;
			width: auto;
		}

		.actions .pill {
			flex: 1;
		}
	}
</style>
