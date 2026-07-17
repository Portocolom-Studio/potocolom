<script lang="ts">
	import { AreaChart } from 'layerchart';
	import { scaleTime } from 'd3-scale';
	import { curveMonotoneX } from 'd3-shape';
	import {
		formatMetricValue,
		formatTimeTick,
		latestPoint,
		type ChartBandPoint,
		type ChartPoint
	} from '$lib/studio-gpu-chart';
	import type { MetricsRange } from '$lib/studio-metrics-range';
	import * as Chart from '$lib/components/ui/chart';

	let {
		title,
		seriesColor,
		points = [],
		bands = [],
		windowStartMs,
		windowEndMs,
		range = '5m',
		unit = '%',
		live = false,
		emptyHint
	}: {
		title: string;
		seriesColor: string;
		points?: ChartPoint[];
		bands?: ChartBandPoint[];
		windowStartMs: number;
		windowEndMs: number;
		range?: MetricsRange;
		unit?: string;
		live?: boolean;
		emptyHint?: string;
	} = $props();

	type Row = { ts: Date; value: number; min?: number; max?: number };

	const rows = $derived.by((): Row[] => {
		if (bands.length > 0) {
			return bands.map((band) => ({
				ts: new Date(band.ts),
				value: band.mean,
				min: band.min,
				max: band.max
			}));
		}
		return points.map((point) => ({ ts: new Date(point.ts), value: point.value }));
	});

	const hasBands = $derived(bands.length > 0);
	const latest = $derived(
		latestPoint(hasBands ? bands.map((band) => ({ ts: band.ts, value: band.mean })) : points)
	);

	const chartConfig = $derived({
		value: { label: title, color: seriesColor }
	} satisfies Chart.ChartConfig);

	const series = $derived.by(() => {
		const main = {
			key: 'value',
			label: title,
			value: (d: Row) => d.value,
			color: seriesColor
		};
		if (!hasBands) return [main];
		return [
			{
				key: 'band',
				label: `${title} min/max`,
				value: (d: Row) => d.max ?? d.value,
				color: seriesColor,
				props: {
					y0: (d: Row) => d.min ?? d.value,
					y1: (d: Row) => d.max ?? d.value,
					'fill-opacity': 0.12,
					line: { class: 'stroke-none' },
					motion: 'tween' as const
				}
			},
			main
		];
	});
</script>

<div class="flex flex-col gap-2">
	<div class="flex items-baseline justify-between gap-2">
		<span class="text-muted-foreground flex items-center gap-1.5 text-xs font-medium">
			{#if live}
				<span class="relative flex size-1.5" aria-hidden="true">
					<span
						class="absolute inline-flex h-full w-full animate-ping opacity-60"
						style={`background: ${seriesColor}`}
					></span>
					<span class="relative inline-flex size-1.5" style={`background: ${seriesColor}`}></span>
				</span>
			{/if}
			{title}
		</span>
		{#if latest}
			<span class="text-lg font-semibold tabular-nums">
				{formatMetricValue(latest.value, unit)}
			</span>
		{/if}
	</div>

	{#if rows.length === 0}
		<div class="text-muted-foreground flex h-[160px] items-center justify-center text-xs">
			{emptyHint ?? '-'}
		</div>
	{:else}
		<Chart.Container config={chartConfig} class="h-[160px] w-full">
			<AreaChart
				data={rows}
				x={(d: Row) => d.ts}
				xScale={scaleTime()}
				xDomain={[new Date(windowStartMs), new Date(windowEndMs)]}
				yDomain={unit === '%' ? [0, 100] : undefined}
				{series}
				rule={false}
				props={{
					area: {
						curve: curveMonotoneX,
						'fill-opacity': 0.25,
						line: { class: 'stroke-[1.5px]' },
						motion: 'tween'
					},
					xAxis: {
						format: (d: Date) => formatTimeTick(+d, range),
						ticks: 4
					},
					yAxis: {
						format: (v: number) => `${v}${unit === '%' ? '%' : ''}`,
						ticks: 3
					},
					grid: { class: 'stroke-border/50' },
					highlight: { points: { r: 3 } }
				}}
			>
				{#snippet tooltip()}
					<Chart.Tooltip indicator="line" labelFormatter={(d: Date) => formatTimeTick(+d, range)} />
				{/snippet}
			</AreaChart>
		</Chart.Container>
	{/if}
</div>
