<script lang="ts">
	import { collageImages, collageLandingSources } from '$lib/collage-images';
	import { createRng } from '$lib/latent-canvas-scene';

	type Tile = {
		id: string;
		left: number;
		top: number;
		size: number;
		cx: number;
		cy: number;
		rotate: number;
		driftDelay: number;
		driftDuration: number;
		src: string;
		srcset: string;
	};

	/** Cursor influence radius in px; tiles inside it scale and brighten. */
	const RADIUS = 380;

	let field: HTMLElement | null = null;
	let tiles = $state.raw<Tile[]>([]);
	let pointer = $state.raw<{ x: number; y: number } | null>(null);
	// Dimmer on small screens where the copy spans the whole field.
	let baseOpacity = $state(0.32);

	// Seeded shuffle so the field looks the same on every load.
	const images = (() => {
		const rng = createRng(42);
		const list = [...collageImages];
		for (let i = list.length - 1; i > 0; i--) {
			const j = Math.floor(rng() * (i + 1));
			[list[i], list[j]] = [list[j], list[i]];
		}
		return list;
	})();

	function buildTiles(width: number, height: number): Tile[] {
		const rng = createRng(97);
		const cell = width < 640 ? 120 : 176;
		// Overscan one row/col and shift the grid so tiles bleed past the edges.
		const cols = Math.ceil(width / cell) + 1;
		const rows = Math.ceil(height / cell) + 1;
		const result: Tile[] = [];
		for (let row = 0; row < rows; row++) {
			// Brick-offset alternate rows so the field reads collage, not grid.
			const rowShift = row % 2 ? -cell * 0.5 : 0;
			for (let col = 0; col < cols; col++) {
				const image = images[(row * cols + col) % images.length];
				const size = cell - 14;
				const cx = col * cell + cell * 0.15 + rowShift + (rng() - 0.5) * cell * 0.2;
				const cy = row * cell + cell * 0.15 + (rng() - 0.5) * cell * 0.2;
				result.push({
					id: `${col}-${row}`,
					left: cx - size / 2,
					top: cy - size / 2,
					size,
					cx,
					cy,
					rotate: (rng() - 0.5) * 8,
					driftDelay: -rng() * 10,
					driftDuration: 6 + rng() * 5,
					...collageLandingSources(image)
				});
			}
		}
		return result;
	}

	function observe(node: HTMLElement) {
		field = node;
		let lastWidth = 0;
		let lastHeight = 0;
		const observer = new ResizeObserver(([entry]) => {
			const { width, height } = entry.contentRect;
			// Overscan absorbs small changes (mobile URL-bar collapse); only
			// rebuild on real resizes so tiles do not churn mid-scroll.
			if (Math.abs(width - lastWidth) < 64 && Math.abs(height - lastHeight) < 64) return;
			lastWidth = width;
			lastHeight = height;
			baseOpacity = width < 640 ? 0.22 : 0.32;
			tiles = buildTiles(width, height);
		});
		observer.observe(node);
		return () => {
			observer.disconnect();
			if (frame) cancelAnimationFrame(frame);
			field = null;
		};
	}

	let pendingX = 0;
	let pendingY = 0;
	let frame = 0;

	function commitPointer() {
		frame = 0;
		if (!field) return;
		const rect = field.getBoundingClientRect();
		const x = pendingX - rect.left;
		const y = pendingY - rect.top;
		const inside =
			x > -RADIUS && y > -RADIUS && x < rect.width + RADIUS && y < rect.height + RADIUS;
		pointer = inside ? { x, y } : null;
	}

	function onPointerMove(event: PointerEvent) {
		pendingX = event.clientX;
		pendingY = event.clientY;
		if (!frame) frame = requestAnimationFrame(commitPointer);
	}

	/** 0..1 proximity boost with quadratic falloff. */
	function boost(tile: Tile): number {
		if (!pointer) return 0;
		const t = 1 - Math.hypot(tile.cx - pointer.x, tile.cy - pointer.y) / RADIUS;
		return t <= 0 ? 0 : t * t;
	}
</script>

<svelte:window onpointermove={onPointerMove} />

<!-- isolate keeps boosted tiles' z-index below the fade mask and hero copy -->
<div class="pointer-events-none relative isolate size-full" aria-hidden="true" {@attach observe}>
	{#each tiles as tile (tile.id)}
		{@const lift = boost(tile)}
		<figure
			class="tile border-border/60 absolute m-0 overflow-hidden rounded-lg border"
			style:left="{tile.left}px"
			style:top="{tile.top}px"
			style:width="{tile.size}px"
			style:height="{tile.size}px"
			style:rotate="{tile.rotate}deg"
			style:--drift-delay="{tile.driftDelay}s"
			style:--drift-duration="{tile.driftDuration}s"
			style:scale={1 + 0.55 * lift}
			style:opacity={baseOpacity + 0.6 * lift}
			style:z-index={Math.round(lift * 24)}
		>
			<img
				src={tile.src}
				srcset={tile.srcset}
				sizes="176px"
				alt=""
				class="size-full object-cover"
				loading="eager"
				decoding="async"
				draggable="false"
			/>
		</figure>
	{/each}
</div>

<style>
	.tile {
		animation: hero-tile-drift var(--drift-duration) ease-in-out infinite alternate;
		animation-delay: var(--drift-delay);
		transition:
			scale 260ms cubic-bezier(0.22, 1, 0.36, 1),
			opacity 260ms ease;
		will-change: scale, opacity, translate;
	}

	@keyframes hero-tile-drift {
		from {
			translate: 0 -5px;
		}
		to {
			translate: 0 5px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.tile {
			animation: none;
			transition: none;
		}
	}
</style>
