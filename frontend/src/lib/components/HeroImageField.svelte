<script lang="ts">
	import heroImages from '$lib/hero-images.json';
	import { createRng } from '$lib/latent-canvas-scene';

	type Tile = {
		id: string;
		left: number;
		top: number;
		rotate: number;
		file: string;
	};
	type Row = {
		top: number;
		phase: number;
		tiles: Tile[];
	};

	/** Cursor influence radius in px; tiles inside it scale and brighten. */
	const RADIUS = 340;
	/** Horizontal flow speed in px/s; eases to 0 while the pointer is over the hero. */
	const BASE_SPEED = 22;

	let field: HTMLElement | null = null;
	let rows = $state.raw<Row[]>([]);
	let pointer = $state.raw<{ x: number; y: number } | null>(null);
	// Dimmer on small screens where the copy spans the whole field.
	let baseOpacity = $state(0.32);
	let cell = $state(128);
	const size = $derived(cell - 12);

	let rowEls = $state.raw<HTMLElement[]>([]);
	let travel = 0;
	let speed = 0;
	let dirty = false;
	let reducedMotion = false;

	// Seeded shuffle so the field looks the same on every load.
	const pool = (() => {
		const rng = createRng(42);
		const list = [...heroImages];
		for (let i = list.length - 1; i > 0; i--) {
			const j = Math.floor(rng() * (i + 1));
			[list[i], list[j]] = [list[j], list[i]];
		}
		return list;
	})();

	const stripWidth = $derived(pool.length * cell);

	function buildRows(width: number, height: number): Row[] {
		const rng = createRng(97);
		const rowCount = Math.ceil(height / cell) + 1;
		// Each row cycles the full pool, rotated by a per-row stride, so an
		// image never repeats within a row and duplicates in neighboring rows
		// stay several columns apart. Same speed everywhere keeps it that way.
		// ponytail: every row carries the whole pool twice (copies for the
		// wrap); trim rows to a viewport-sized window if DOM count ever hurts.
		const stride = Math.max(1, Math.floor(pool.length / rowCount));
		const result: Row[] = [];
		for (let r = 0; r < rowCount; r++) {
			const tiles: Tile[] = [];
			for (let i = 0; i < pool.length; i++) {
				tiles.push({
					id: `${r}-${i}`,
					left: i * cell + (rng() - 0.5) * cell * 0.2,
					top: (rng() - 0.5) * cell * 0.2,
					rotate: (rng() - 0.5) * 8,
					file: pool[(i + r * stride) % pool.length]
				});
			}
			result.push({
				top: r * cell - cell * 0.35,
				phase: (r % 2 ? cell * 0.5 : 0) + rng() * cell * 0.25,
				tiles
			});
		}
		return result;
	}

	function observe(node: HTMLElement) {
		field = node;
		reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		let lastWidth = 0;
		let lastHeight = 0;
		const observer = new ResizeObserver(([entry]) => {
			const { width, height } = entry.contentRect;
			// Overscan absorbs small changes (mobile URL-bar collapse); only
			// rebuild on real resizes so tiles do not churn mid-scroll.
			if (Math.abs(width - lastWidth) < 64 && Math.abs(height - lastHeight) < 64) return;
			lastWidth = width;
			lastHeight = height;
			cell = width < 640 ? 104 : 128;
			baseOpacity = width < 640 ? 0.22 : 0.32;
			rowEls = [];
			rows = buildRows(width, height);
			dirty = true;
		});
		observer.observe(node);

		let last = performance.now();
		let loopFrame = requestAnimationFrame(function loop(now) {
			const dt = Math.min((now - last) / 1000, 0.1);
			last = now;
			const target = reducedMotion || pointer ? 0 : BASE_SPEED;
			speed += (target - speed) * 0.06;
			if (speed > 0.05 || dirty) {
				dirty = false;
				travel += speed * dt;
				for (let r = 0; r < rows.length; r++) {
					const el = rowEls[r];
					if (el) {
						el.style.transform = `translateX(${-((rows[r].phase + travel) % stripWidth)}px)`;
					}
				}
			}
			loopFrame = requestAnimationFrame(loop);
		});

		return () => {
			observer.disconnect();
			cancelAnimationFrame(loopFrame);
			if (pointerFrame) cancelAnimationFrame(pointerFrame);
			field = null;
		};
	}

	let pendingX = 0;
	let pendingY = 0;
	let pointerFrame = 0;

	function commitPointer() {
		pointerFrame = 0;
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
		if (!pointerFrame) pointerFrame = requestAnimationFrame(commitPointer);
	}

	/** 0..1 proximity boost with quadratic falloff. */
	function boost(row: Row, tile: Tile, copy: number): number {
		if (!pointer) return 0;
		const offset = (row.phase + travel) % stripWidth;
		const cx = tile.left + copy * stripWidth - offset + size / 2;
		const cy = row.top + tile.top + size / 2;
		const t = 1 - Math.hypot(cx - pointer.x, cy - pointer.y) / RADIUS;
		return t <= 0 ? 0 : t * t;
	}

	/** Raise the row nearest the cursor so magnified tiles overlap neighbors. */
	function rowZ(row: Row): number {
		if (!pointer) return 0;
		const distance = Math.abs(pointer.y - (row.top + cell / 2));
		return Math.max(0, 6 - Math.round(distance / cell) * 2);
	}
</script>

<svelte:window onpointermove={onPointerMove} />

<!-- isolate keeps boosted tiles' z-index below the fade mask and hero copy -->
<div
	class="pointer-events-none relative isolate size-full overflow-hidden"
	aria-hidden="true"
	{@attach observe}
>
	{#each rows as row, r (row.top)}
		<div class="absolute left-0 w-full" style:top="{row.top}px" style:z-index={rowZ(row)}>
			<div bind:this={rowEls[r]}>
				{#each [0, 1] as copy (copy)}
					{#each row.tiles as tile (tile.id)}
						{@const lift = boost(row, tile, copy)}
						<figure
							class="tile border-border/60 absolute m-0 overflow-hidden rounded-lg border"
							style:left="{tile.left + copy * stripWidth}px"
							style:top="{tile.top}px"
							style:width="{size}px"
							style:height="{size}px"
							style:rotate="{tile.rotate}deg"
							style:scale={1 + 0.55 * lift}
							style:opacity={baseOpacity + 0.6 * lift}
							style:z-index={Math.round(lift * 24)}
						>
							<img
								src="/images/hero/{tile.file}"
								alt=""
								class="size-full object-cover"
								loading="eager"
								decoding="async"
								draggable="false"
							/>
						</figure>
					{/each}
				{/each}
			</div>
		</div>
	{/each}
</div>

<style>
	.tile {
		transition:
			scale 260ms cubic-bezier(0.22, 1, 0.36, 1),
			opacity 260ms ease;
	}

	@media (prefers-reduced-motion: reduce) {
		.tile {
			transition: none;
		}
	}
</style>
