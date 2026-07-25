<script lang="ts">
	import { resolve } from '$app/paths';
	import { collageImages, collageLandingSources } from '$lib/collage-images';
	import { t } from '$lib/i18n.svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const tiles = collageImages.slice(0, 24);

	let active = $state<string | null>(null);
	let pinned = $state<string | null>(null);

	const shown = $derived(pinned ?? active);
	const shownTile = $derived(tiles.find((tile) => tile.file === shown) ?? null);
</script>

<div class="krea salon" class:focused={shown !== null}>
	<div class="grid">
		{#each tiles as tile (tile.file)}
			{@const sources = collageLandingSources(tile)}
			<button
				type="button"
				class="tile"
				class:lit={shown === tile.file}
				aria-pressed={pinned === tile.file}
				onmouseenter={() => (active = tile.file)}
				onmouseleave={() => (active = null)}
				onfocus={() => (active = tile.file)}
				onblur={() => (active = null)}
				onclick={() => (pinned = pinned === tile.file ? null : tile.file)}
			>
				<img src={sources.src} srcset={sources.srcset} alt={tile.alt} loading="lazy" />
			</button>
		{/each}
	</div>

	<header>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
	</header>

	<div class="plate">
		{#if shownTile}
			<p class="label">{t('gallery.kicker')}</p>
			<h2>{shownTile.alt}</h2>
			<p class="hint">{pinned ? t('gallery.prompts_hint') : t('gallery.sub')}</p>
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

	.grid {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		grid-auto-rows: 25svh;
		height: 100%;
	}

	.tile {
		position: relative;
		min-width: 0;
		padding: 0;
		border: 0;
		background: none;
		cursor: pointer;
		overflow: clip;
	}

	.tile img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
		transition:
			opacity 320ms var(--k-ease),
			transform 420ms var(--k-ease),
			filter 320ms var(--k-ease);
	}

	.salon.focused .tile img {
		opacity: 0.32;
		filter: saturate(0.4);
	}

	.salon.focused .tile.lit img {
		opacity: 1;
		filter: none;
		transform: scale(1.04);
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

	@media (min-width: 64rem) {
		.grid {
			grid-template-columns: repeat(6, minmax(0, 1fr));
			grid-auto-rows: 25svh;
		}
	}

	@media (max-width: 40rem) {
		.grid {
			grid-template-columns: repeat(3, minmax(0, 1fr));
			grid-auto-rows: 16svh;
		}

		.plate {
			inset-inline: 0.75rem;
			width: auto;
		}

		.actions .pill {
			flex: 1;
		}
	}
</style>
