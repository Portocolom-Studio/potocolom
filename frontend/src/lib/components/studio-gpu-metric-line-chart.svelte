<script lang="ts">
	import {
		areaPath,
		bandAreaPath,
		CHART_MARGINS,
		formatMetricValue,
		formatRangeCaption,
		formatTimeTick,
		latestPoint,
		linePath,
		nearestPointIndex,
		rollingMean,
		timeTicks,
		tsFromPlotX,
		xScale,
		yScale,
		type ChartBandPoint,
		type ChartPoint
	} from '$lib/studio-gpu-chart';
	import type { MetricsRange } from '$lib/studio-metrics-range';

	let {
		title,
		seriesColor,
		points = [],
		windowStartMs,
		windowEndMs,
		range = '5m',
		width = 640,
		height = 168,
		unit = '%',
		yUnitLabel,
		rollingWindow = 8,
		live = false,
		bands = [],
		emptyHint
	}: {
		title: string;
		seriesColor: string;
		points?: ChartPoint[];
		bands?: ChartBandPoint[];
		windowStartMs: number;
		windowEndMs: number;
		range?: MetricsRange;
		width?: number;
		height?: number;
		unit?: string;
		yUnitLabel?: string;
		rollingWindow?: number;
		live?: boolean;
		emptyHint?: string;
	} = $props();

	const m = CHART_MARGINS;
	const plotLeft = m.left;
	const plotTop = m.top;
	const plotWidth = $derived(width - m.left - m.right);
	const plotHeight = $derived(height - m.top - m.bottom);
	const yTicks = [0, 25, 50, 75, 100];
	const xTicks = $derived(timeTicks(windowStartMs, windowEndMs, 6));
	const meanPoints = $derived(rollingMean(points, rollingWindow));
	const seriesPoints = $derived(bands.length > 0 ? points : meanPoints);
	const latest = $derived(latestPoint(seriesPoints));
	const gradientId = $derived(`gpu-metric-${title.replace(/\W+/g, '-').toLowerCase()}`);
	const bandGradientId = $derived(`${gradientId}-band`);
	const rangeCaption = $derived(formatRangeCaption(windowStartMs, windowEndMs, range));
	const axisUnit = $derived(yUnitLabel ?? unit);

	let hoverIndex = $state<number | null>(null);
	let tooltipLeft = $state(0);
	let tooltipTop = $state(0);
	let tooltipFlip = $state(false);
	let plotHost: HTMLDivElement | undefined = $state();

	const hoverPoint = $derived(hoverIndex == null ? null : (seriesPoints[hoverIndex] ?? null));
	const hoverRaw = $derived(hoverIndex == null ? null : (points[hoverIndex] ?? hoverPoint));

	function clampTooltip(clientX: number, clientY: number) {
		if (!plotHost) return;
		const rect = plotHost.getBoundingClientRect();
		const localX = clientX - rect.left;
		const localY = clientY - rect.top;
		const tooltipW = 148;
		const tooltipH = 56;
		tooltipFlip = localX > rect.width - tooltipW - 12;
		tooltipLeft = tooltipFlip ? localX - tooltipW - 8 : localX + 12;
		tooltipTop = Math.max(8, Math.min(localY - tooltipH / 2, rect.height - tooltipH - 8));
	}

	function onPointerMove(event: PointerEvent) {
		if (!plotHost || seriesPoints.length === 0) {
			hoverIndex = null;
			return;
		}
		const svg = plotHost.querySelector('svg');
		if (!svg) return;
		const rect = svg.getBoundingClientRect();
		const x = ((event.clientX - rect.left) / Math.max(rect.width, 1)) * width;
		const ts = tsFromPlotX(x, windowStartMs, windowEndMs, plotLeft, plotWidth);
		hoverIndex = nearestPointIndex(seriesPoints, ts);
		clampTooltip(event.clientX, event.clientY);
	}

	function onPointerDown(event: PointerEvent) {
		onPointerMove(event);
	}

	function onPointerLeave() {
		hoverIndex = null;
	}
</script>

<figure class="flex flex-col gap-1.5">
	<figcaption class="flex items-baseline justify-between gap-3 px-1">
		<span class="text-sm font-medium">{title}</span>
		{#if latest}
			<span class="text-foreground flex items-center gap-1.5 font-mono text-sm tabular-nums">
				<span
					class="inline-block size-2 shrink-0 rounded-sm"
					style={`background: ${seriesColor}`}
					aria-hidden="true"
				></span>
				{formatMetricValue(latest.value, unit)}
			</span>
		{:else}
			<span class="text-muted-foreground font-mono text-sm tabular-nums">-</span>
		{/if}
	</figcaption>

	<div
		class="relative w-full"
		bind:this={plotHost}
		class:cursor-crosshair={points.length > 0}
		onpointermove={onPointerMove}
		onpointerdown={onPointerDown}
		onpointerleave={onPointerLeave}
		role="presentation"
	>
		<svg
			{width}
			{height}
			viewBox="0 0 {width} {height}"
			class="w-full max-w-full"
			role="img"
			aria-label={title}
		>
			<defs>
				<linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
					<stop offset="0%" stop-color={seriesColor} stop-opacity="0.28" />
					<stop offset="100%" stop-color={seriesColor} stop-opacity="0.02" />
				</linearGradient>
			</defs>

			<text
				x={plotLeft - 6}
				y={plotTop - 6}
				text-anchor="end"
				class="fill-muted-foreground text-[10px]"
			>
				{axisUnit}
			</text>

			{#each yTicks as tick (tick)}
				{@const y = yScale(tick, plotTop, plotHeight)}
				<line
					x1={plotLeft}
					y1={y}
					x2={plotLeft + plotWidth}
					y2={y}
					class="stroke-border/70"
					stroke-width="1"
					vector-effect="non-scaling-stroke"
				/>
				<text
					x={plotLeft - 8}
					y={y + 4}
					text-anchor="end"
					class="fill-muted-foreground font-mono text-[10px] tabular-nums"
				>
					{tick}
				</text>
			{/each}

			{#if points.length > 0}
				{#if bands.length > 0}
					<path
						d={bandAreaPath(
							bands,
							windowStartMs,
							windowEndMs,
							plotLeft,
							plotWidth,
							plotTop,
							plotHeight
						)}
						fill={seriesColor}
						fill-opacity="0.12"
					/>
					<path
						d={linePath(
							seriesPoints,
							windowStartMs,
							windowEndMs,
							plotLeft,
							plotWidth,
							plotTop,
							plotHeight
						)}
						fill="none"
						stroke={seriesColor}
						stroke-width="2"
						stroke-linejoin="round"
						stroke-linecap="round"
						vector-effect="non-scaling-stroke"
					/>
				{:else}
					<path
						d={linePath(
							points,
							windowStartMs,
							windowEndMs,
							plotLeft,
							plotWidth,
							plotTop,
							plotHeight
						)}
						fill="none"
						stroke={seriesColor}
						stroke-width="1.5"
						stroke-opacity="0.3"
						stroke-linejoin="round"
						stroke-linecap="round"
						vector-effect="non-scaling-stroke"
					/>
					<path
						d={areaPath(
							meanPoints,
							windowStartMs,
							windowEndMs,
							plotLeft,
							plotWidth,
							plotTop,
							plotHeight
						)}
						fill={`url(#${gradientId})`}
					/>
					<path
						d={linePath(
							meanPoints,
							windowStartMs,
							windowEndMs,
							plotLeft,
							plotWidth,
							plotTop,
							plotHeight
						)}
						fill="none"
						stroke={seriesColor}
						stroke-width="2"
						stroke-linejoin="round"
						stroke-linecap="round"
						vector-effect="non-scaling-stroke"
					/>
				{/if}

				{#if latest}
					<circle
						cx={xScale(latest.ts, windowStartMs, windowEndMs, plotLeft, plotWidth)}
						cy={yScale(latest.value, plotTop, plotHeight)}
						r="3.5"
						class="fill-background"
						class:chart-live-pulse={live}
						stroke={seriesColor}
						stroke-width="2"
						vector-effect="non-scaling-stroke"
					/>
				{/if}

				{#if hoverPoint}
					{@const hx = xScale(hoverPoint.ts, windowStartMs, windowEndMs, plotLeft, plotWidth)}
					{@const hy = yScale(hoverPoint.value, plotTop, plotHeight)}
					<line
						x1={hx}
						y1={plotTop}
						x2={hx}
						y2={plotTop + plotHeight}
						stroke="var(--muted-foreground)"
						stroke-opacity="0.45"
						stroke-width="1"
						vector-effect="non-scaling-stroke"
					/>
					<circle
						cx={hx}
						cy={hy}
						r="3.5"
						class="fill-background"
						stroke={seriesColor}
						stroke-width="2"
						vector-effect="non-scaling-stroke"
					/>
				{/if}
			{:else}
				<text
					x={plotLeft + plotWidth / 2}
					y={plotTop + plotHeight / 2}
					text-anchor="middle"
					class="fill-muted-foreground text-xs"
				>
					{emptyHint ?? 'Waiting for samples...'}
				</text>
			{/if}

			{#each xTicks as tick, index (index)}
				<text
					x={xScale(tick, windowStartMs, windowEndMs, plotLeft, plotWidth)}
					y={height - 8}
					text-anchor={index === 0 ? 'start' : index === xTicks.length - 1 ? 'end' : 'middle'}
					class="fill-muted-foreground font-mono text-[10px] tabular-nums"
				>
					{formatTimeTick(tick, range)}
				</text>
			{/each}
		</svg>

		{#if hoverPoint && hoverRaw}
			<div
				class="bg-popover text-popover-foreground border-border pointer-events-none absolute z-10 rounded-md border px-2 py-1 text-xs shadow-sm"
				style={`left: ${tooltipLeft}px; top: ${tooltipTop}px;`}
				role="tooltip"
			>
				<div class="text-muted-foreground font-mono tabular-nums">
					{formatTimeTick(hoverPoint.ts, '5m')}
				</div>
				<div class="text-foreground mt-0.5 flex items-center gap-1.5 font-mono tabular-nums">
					<span
						class="inline-block size-2 shrink-0 rounded-sm"
						style={`background: ${seriesColor}`}
						aria-hidden="true"
					></span>
					<span>{title}</span>
					<span class="ms-auto">{formatMetricValue(hoverRaw.value, unit)}</span>
				</div>
			</div>
		{/if}
	</div>

	<p class="text-muted-foreground px-1 text-[10px] tabular-nums">{rangeCaption}</p>
</figure>

<style>
	@keyframes chart-live-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.45;
		}
	}

	:global(.chart-live-pulse) {
		animation: chart-live-pulse 2s ease-in-out infinite;
	}
</style>
