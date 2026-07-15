<script lang="ts">
	import {
		areaPath,
		CHART_MARGINS,
		formatTimeTick,
		latestPoint,
		linePath,
		timeTicks,
		xScale,
		yScale,
		type ChartPoint
	} from '$lib/studio-gpu-chart';

	let {
		title,
		color,
		points = [],
		windowStartMs,
		windowEndMs,
		width = 640,
		height = 168,
		unit = '%',
		emptyHint
	}: {
		title: string;
		color: string;
		points?: ChartPoint[];
		windowStartMs: number;
		windowEndMs: number;
		width?: number;
		height?: number;
		unit?: string;
		emptyHint?: string;
	} = $props();

	const m = CHART_MARGINS;
	const plotLeft = m.left;
	const plotTop = m.top;
	const plotWidth = $derived(width - m.left - m.right);
	const plotHeight = $derived(height - m.top - m.bottom);
	const yTicks = [0, 25, 50, 75, 100];
	const xTicks = $derived(timeTicks(windowStartMs, windowEndMs, 6));
	const latest = $derived(latestPoint(points));
	const gradientId = $derived(`gpu-metric-${title.replace(/\W+/g, '-').toLowerCase()}`);
</script>

<figure class="flex flex-col gap-2">
	<figcaption class="flex items-baseline justify-between gap-3 px-1">
		<span class="text-sm font-medium">{title}</span>
		{#if latest}
			<span class="font-mono text-sm tabular-nums" style={`color: ${color}`}>
				{Math.round(latest.value)}{unit}
			</span>
		{:else}
			<span class="text-muted-foreground font-mono text-sm tabular-nums">-</span>
		{/if}
	</figcaption>
	<svg
		{width}
		{height}
		viewBox="0 0 {width} {height}"
		class="bg-muted/15 border-border w-full max-w-full rounded-lg border"
		role="img"
		aria-label={title}
	>
		<defs>
			<linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
				<stop offset="0%" stop-color={color} stop-opacity="0.28" />
				<stop offset="100%" stop-color={color} stop-opacity="0.02" />
			</linearGradient>
		</defs>

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

		{#each xTicks as tick, index (index)}
			{@const x = xScale(tick, windowStartMs, windowEndMs, plotLeft, plotWidth)}
			<line
				x1={x}
				y1={plotTop}
				x2={x}
				y2={plotTop + plotHeight}
				class="stroke-border/40"
				stroke-width="1"
				stroke-dasharray="3 4"
				vector-effect="non-scaling-stroke"
			/>
		{/each}

		<rect
			x={plotLeft}
			y={plotTop}
			width={plotWidth}
			height={plotHeight}
			fill="none"
			class="stroke-border/80"
			stroke-width="1"
			vector-effect="non-scaling-stroke"
		/>

		{#if points.length > 0}
			<path
				d={areaPath(points, windowStartMs, windowEndMs, plotLeft, plotWidth, plotTop, plotHeight)}
				fill={`url(#${gradientId})`}
			/>
			<path
				d={linePath(points, windowStartMs, windowEndMs, plotLeft, plotWidth, plotTop, plotHeight)}
				fill="none"
				stroke={color}
				stroke-width="2"
				stroke-linejoin="round"
				stroke-linecap="round"
				vector-effect="non-scaling-stroke"
			/>
			{#if latest}
				<circle
					cx={xScale(latest.ts, windowStartMs, windowEndMs, plotLeft, plotWidth)}
					cy={yScale(latest.value, plotTop, plotHeight)}
					r="3.5"
					fill="var(--background)"
					stroke={color}
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
				{formatTimeTick(tick)}
			</text>
		{/each}
	</svg>
</figure>
