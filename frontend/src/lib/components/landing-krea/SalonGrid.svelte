<script lang="ts">
	export type SalonTile = {
		key: string;
		alt: string;
		src: string;
		srcset: string;
	};

	let {
		tiles,
		onactive
	}: {
		tiles: SalonTile[];
		/** Fires with the tile under focus, or null when nothing is engaged. */
		onactive?: (tile: SalonTile | null) => void;
	} = $props();

	let hovered = $state<string | null>(null);
	let pinned = $state<string | null>(null);

	const shown = $derived(pinned ?? hovered);

	$effect(() => {
		onactive?.(tiles.find((tile) => tile.key === shown) ?? null);
	});
</script>

<!-- Column counts are literal in CSS on purpose: repeat(var(--n)) was resolving
     to broken layouts (2 visible columns, clipped rows) in the built page. -->
<div class="salon-grid" class:focused={shown !== null}>
	{#each tiles as tile (tile.key)}
		<button
			type="button"
			class:lit={shown === tile.key}
			aria-pressed={pinned === tile.key}
			onmouseenter={() => (hovered = tile.key)}
			onmouseleave={() => (hovered = null)}
			onfocus={() => (hovered = tile.key)}
			onblur={() => (hovered = null)}
			onclick={() => (pinned = pinned === tile.key ? null : tile.key)}
		>
			<img
				src={tile.src}
				srcset={tile.srcset}
				sizes="(max-width: 40rem) 33vw, 16vw"
				alt={tile.alt}
				loading="lazy"
			/>
		</button>
	{/each}
</div>

<style>
	.salon-grid {
		display: grid;
		grid-template-columns: repeat(6, minmax(0, 1fr));
		grid-auto-rows: auto;
		width: 100%;
		height: auto;
		overflow: visible;
	}

	button {
		position: relative;
		min-width: 0;
		width: 100%;
		aspect-ratio: 1 / 1;
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
			grid-template-columns: repeat(3, minmax(0, 1fr));
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
