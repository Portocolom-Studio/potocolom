<script lang="ts">
	import { formatMs, type CategoryLineSeries } from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';

	let {
		categories,
		series
	}: {
		categories: string[];
		series: CategoryLineSeries[];
	} = $props();

	let chartEl = $state<HTMLDivElement | null>(null);
	let tip = $state<{
		x: number;
		y: number;
		categoryIndex: number;
		seriesIndex?: number;
	} | null>(null);

	const padX = 50;
	const padY = 24;
	const inner = 520;
	const viewW = inner + padX * 2;
	const viewH = inner + padY * 2;
	const cx = padX + inner / 2;
	const cy = padY + inner / 2;
	const radius = inner * 0.34;
	const labelRadius = inner * 0.44;

	const categoryBounds = $derived(
		categories.map((category) => {
			const values = series
				.map((row) => row.points.find((point) => point.category === category)?.avg_gpu_ms ?? 0)
				.filter((ms) => ms > 0);
			const max = Math.max(...values, 1);
			const min = Math.min(...values, max);
			return { max, min };
		})
	);

	const activeCategory = $derived(tip?.categoryIndex ?? null);

	const gridLevels = [0.25, 0.5, 0.75, 1];

	function axisAngle(index: number): number {
		return -Math.PI / 2 + ((2 * Math.PI) / categories.length) * index;
	}

	function polar(index: number, scale: number): { x: number; y: number } {
		const angle = axisAngle(index);
		return {
			x: cx + Math.cos(angle) * radius * scale,
			y: cy + Math.sin(angle) * radius * scale
		};
	}

	function ringPath(scale: number): string {
		return (
			categories
				.map((_, index) => {
					const point = polar(index, scale);
					return `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`;
				})
				.join(' ') + ' Z'
		);
	}

	function score(ms: number, categoryIndex: number): number {
		const { max, min } = categoryBounds[categoryIndex];
		if (max <= min) return 1;
		return (max - ms) / (max - min);
	}

	function gpuMs(row: CategoryLineSeries, categoryIndex: number): number {
		const category = categories[categoryIndex];
		return row.points.find((point) => point.category === category)?.avg_gpu_ms ?? 0;
	}

	function vertexPoint(row: CategoryLineSeries, categoryIndex: number): { x: number; y: number } {
		return polar(categoryIndex, score(gpuMs(row, categoryIndex), categoryIndex));
	}

	function seriesPath(row: CategoryLineSeries): string {
		return (
			categories
				.map((category, index) => {
					const ms = row.points.find((point) => point.category === category)?.avg_gpu_ms ?? 0;
					const point = polar(index, score(ms, index));
					return `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`;
				})
				.join(' ') + ' Z'
		);
	}

	function labelPoint(index: number): { x: number; y: number; anchor: string } {
		const angle = axisAngle(index);
		const x = cx + Math.cos(angle) * labelRadius;
		const y = cy + Math.sin(angle) * labelRadius;
		const anchor =
			Math.abs(Math.cos(angle)) < 0.2 ? 'middle' : Math.cos(angle) > 0 ? 'start' : 'end';
		return { x, y, anchor };
	}

	function wedgePath(index: number): string {
		const next = (index + 1) % categories.length;
		const start = polar(index, 1);
		const end = polar(next, 1);
		return `M${cx},${cy} L${start.x.toFixed(1)},${start.y.toFixed(1)} L${end.x.toFixed(1)},${end.y.toFixed(1)} Z`;
	}

	function clientToSvg(event: PointerEvent, svg: SVGSVGElement): { x: number; y: number } | null {
		const point = svg.createSVGPoint();
		point.x = event.clientX;
		point.y = event.clientY;
		const matrix = svg.getScreenCTM();
		if (!matrix) return null;
		const local = point.matrixTransform(matrix.inverse());
		return { x: local.x, y: local.y };
	}

	function categoryFromPoint(x: number, y: number): number | null {
		const dx = x - cx;
		const dy = y - cy;
		if (!pointInOuterRing(x, y)) return null;

		let angle = Math.atan2(dy, dx) + Math.PI / 2;
		if (angle < 0) angle += 2 * Math.PI;
		if (angle >= 2 * Math.PI) angle -= 2 * Math.PI;

		return Math.min(categories.length - 1, Math.floor((angle / (2 * Math.PI)) * categories.length));
	}

	function pointInOuterRing(x: number, y: number): boolean {
		const verts = categories.map((_, index) => polar(index, 1));
		let inside = false;
		for (let i = 0, j = verts.length - 1; i < verts.length; j = i++) {
			const xi = verts[i].x;
			const yi = verts[i].y;
			const xj = verts[j].x;
			const yj = verts[j].y;
			const intersect = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi;
			if (intersect) inside = !inside;
		}
		return inside;
	}

	function showTip(event: PointerEvent, categoryIndex: number, seriesIndex?: number) {
		if (!chartEl) return;
		const rect = chartEl.getBoundingClientRect();
		tip = {
			x: event.clientX - rect.left,
			y: event.clientY - rect.top,
			categoryIndex,
			seriesIndex
		};
	}

	function showTipFromChart(event: PointerEvent, seriesIndex?: number) {
		const svg = (event.currentTarget as SVGGraphicsElement).ownerSVGElement;
		if (!svg) return;
		const local = clientToSvg(event, svg);
		if (!local) return;
		const categoryIndex = categoryFromPoint(local.x, local.y);
		if (categoryIndex === null) {
			clearTip();
			return;
		}
		showTip(event, categoryIndex, seriesIndex);
	}

	function handleChartLeave(event: PointerEvent) {
		const related = event.relatedTarget as Node | null;
		if (related && chartEl?.contains(related)) return;
		clearTip();
	}

	function isChartHitTarget(el: Element): boolean {
		const svg = chartEl?.querySelector('svg');
		if (!svg?.contains(el)) return false;
		return Boolean(
			el.closest('path[data-series]') ||
			el.closest('path[role=presentation]') ||
			el.closest('circle[role=presentation]') ||
			el.tagName === 'text'
		);
	}

	function handleInteractiveLeave(event: PointerEvent) {
		const related = event.relatedTarget as Element | null;
		if (!related) {
			clearTip();
			return;
		}
		if (isChartHitTarget(related)) return;
		if (related === chartEl?.querySelector('svg')) {
			clearTip();
			return;
		}
		if (!chartEl?.contains(related)) clearTip();
	}

	function handleSvgBackgroundMove(event: PointerEvent) {
		if (event.target === event.currentTarget) clearTip();
	}

	function clearTip() {
		tip = null;
	}

	function tipRows(categoryIndex: number) {
		return series
			.map((row, seriesIndex) => ({
				seriesIndex,
				model_id: row.model_id,
				color: row.color,
				avg_gpu_ms: gpuMs(row, categoryIndex)
			}))
			.filter((row) => row.avg_gpu_ms > 0)
			.sort((a, b) => a.avg_gpu_ms - b.avg_gpu_ms);
	}
</script>

<div
	bind:this={chartEl}
	class="relative"
	role="group"
	aria-label={t('bench.chart_category')}
	onpointerleave={handleChartLeave}
>
	<svg
		viewBox="0 0 {viewW} {viewH}"
		class="mx-auto block w-full max-w-2xl touch-none"
		role="img"
		aria-label={t('bench.chart_category')}
		onpointermove={handleSvgBackgroundMove}
	>
		{#each gridLevels as level (level)}
			<path
				d={ringPath(level)}
				fill="none"
				class="pointer-events-none stroke-border/70"
				stroke-width="1"
				stroke-dasharray={level < 1 ? '4 4' : '0'}
			/>
		{/each}

		{#each categories as _, index (index)}
			{@const outer = polar(index, 1)}
			<line
				x1={cx}
				y1={cy}
				x2={outer.x}
				y2={outer.y}
				class="pointer-events-none {activeCategory === index
					? 'stroke-primary/50'
					: 'stroke-border/50'}"
				stroke-width={activeCategory === index ? 2 : 1}
			/>
		{/each}

		{#each categories as _, index (index)}
			<path
				d={wedgePath(index)}
				fill="transparent"
				class="cursor-pointer"
				role="presentation"
				onpointerenter={(event) => showTip(event, index)}
				onpointermove={(event) => showTip(event, index)}
				onpointerleave={handleInteractiveLeave}
			/>
		{/each}

		{#each series as row, seriesIndex (row.model_id)}
			<path
				d={seriesPath(row)}
				fill={row.color}
				fill-opacity="0.14"
				stroke={row.color}
				stroke-width="2"
				stroke-linejoin="round"
				class="cursor-pointer"
				role="presentation"
				data-series={seriesIndex}
				onpointerenter={(event) => showTipFromChart(event, seriesIndex)}
				onpointermove={(event) => showTipFromChart(event, seriesIndex)}
				onpointerleave={handleInteractiveLeave}
			/>
		{/each}

		{#each series as row, seriesIndex (row.model_id)}
			{#each categories as _, categoryIndex (categoryIndex)}
				{@const point = vertexPoint(row, categoryIndex)}
				{@const active =
					activeCategory === categoryIndex &&
					(tip?.seriesIndex == null || tip.seriesIndex === seriesIndex)}
				{#if active}
					<circle
						cx={point.x}
						cy={point.y}
						r="4"
						fill={row.color}
						stroke="var(--background)"
						stroke-width="1.5"
						class="pointer-events-none"
					/>
				{/if}
				<circle
					cx={point.x}
					cy={point.y}
					r="12"
					fill="transparent"
					class="cursor-pointer"
					role="presentation"
					onpointerenter={(event) => showTip(event, categoryIndex, seriesIndex)}
					onpointermove={(event) => showTip(event, categoryIndex, seriesIndex)}
					onpointerleave={handleInteractiveLeave}
				/>
			{/each}
		{/each}

		{#each categories as category, index (category)}
			{@const label = labelPoint(index)}
			<text
				x={label.x}
				y={label.y}
				text-anchor={label.anchor}
				dominant-baseline="middle"
				class="fill-muted-foreground cursor-pointer text-[10px] capitalize"
				role="presentation"
				onpointerenter={(event) => showTip(event, index)}
				onpointermove={(event) => showTip(event, index)}
				onpointerleave={handleInteractiveLeave}
			>
				{category}
			</text>
		{/each}
	</svg>

	{#if tip}
		{@const rows = tipRows(tip.categoryIndex)}
		<div
			class="bg-popover text-popover-foreground pointer-events-none absolute z-10 min-w-[9.5rem] rounded-lg border px-3 py-2 shadow-md"
			style:left="{tip.x}px"
			style:top="{tip.y}px"
			style:transform="translate(-50%, calc(-100% - 10px))"
		>
			<p class="text-foreground mb-1.5 text-xs font-semibold capitalize">
				{categories[tip.categoryIndex]}
			</p>
			<ul class="space-y-1">
				{#each rows as row (row.model_id)}
					<li
						class="flex items-center justify-between gap-3 text-xs"
						class:opacity-45={tip.seriesIndex != null && tip.seriesIndex !== row.seriesIndex}
					>
						<span class="flex min-w-0 items-center gap-1.5">
							<span class="inline-block h-2 w-2 shrink-0 rounded-full" style:background={row.color}
							></span>
							<span class="truncate font-mono">{row.model_id}</span>
						</span>
						<span class="shrink-0 font-mono tabular-nums">{formatMs(row.avg_gpu_ms)}</span>
					</li>
				{/each}
			</ul>
		</div>
	{/if}
</div>

<div class="mt-1 flex flex-wrap justify-center gap-x-4 gap-y-1.5">
	{#each series as row (row.model_id)}
		<span class="flex items-center gap-1.5 text-xs">
			<span class="inline-block h-2.5 w-2.5 rounded-full" style:background={row.color}></span>
			<span class="font-mono">{row.model_id}</span>
		</span>
	{/each}
</div>
<p class="text-muted-foreground mt-1.5 text-center text-xs">{t('bench.chart_radar_note')}</p>
