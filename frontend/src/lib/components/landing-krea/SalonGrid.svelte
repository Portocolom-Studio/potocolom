<script lang="ts">
	import { collageLandingSources, type CollageImage } from '$lib/collage-images';

	let {
		tiles,
		columns = 6,
		rows = '25svh',
		onactive
	}: {
		tiles: CollageImage[];
		/** Columns at the widest breakpoint; halved below 40rem. */
		columns?: number;
		rows?: string;
		/** Fires with the tile under focus, or null when nothing is engaged. */
		onactive?: (tile: CollageImage | null) => void;
	} = $props();

	let hovered = $state<string | null>(null);
	let pinned = $state<string | null>(null);

	const shown = $derived(pinned ?? hovered);

	$effect(() => {
		onactive?.(tiles.find((tile) => tile.file === shown) ?? null);
	});
</script>

<div class="salon-grid" class:focused={shown !== null} style="--cols: {columns}; --rows: {rows}">
	{#each tiles as tile (tile.file)}
		{@const sources = collageLandingSources(tile)}
		<button
			type="button"
			class:lit={shown === tile.file}
			aria-pressed={pinned === tile.file}
			onmouseenter={() => (hovered = tile.file)}
			onmouseleave={() => (hovered = null)}
			onfocus={() => (hovered = tile.file)}
			onblur={() => (hovered = null)}
			onclick={() => (pinned = pinned === tile.file ? null : tile.file)}
		>
			<img src={sources.src} srcset={sources.srcset} alt={tile.alt} loading="lazy" />
		</button>
	{/each}
</div>

<style>
	.salon-grid {
		display: grid;
		grid-template-columns: repeat(var(--cols), minmax(0, 1fr));
		grid-auto-rows: var(--rows);
		height: 100%;
	}

	button {
		position: relative;
		min-width: 0;
		padding: 0;
		border: 0;
		background: none;
		cursor: pointer;
		overflow: clip;
	}

	img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
		transition:
			opacity 320ms cubic-bezier(0.16, 1, 0.3, 1),
			transform 420ms cubic-bezier(0.16, 1, 0.3, 1),
			filter 320ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	.focused img {
		opacity: 0.3;
		filter: saturate(0.4);
	}

	.focused .lit img {
		opacity: 1;
		filter: none;
		transform: scale(1.04);
	}

	@media (max-width: 40rem) {
		.salon-grid {
			grid-template-columns: repeat(calc(var(--cols) / 2), minmax(0, 1fr));
		}
	}

	@media (prefers-reduced-motion: reduce) {
		img {
			transition-duration: 0.01ms;
		}

		.focused .lit img {
			transform: none;
		}
	}
</style>
