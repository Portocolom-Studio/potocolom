<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import {
		CHART_MARGINS,
		formatRangeCaption,
		formatTimeTick,
		timeTicks,
		xScale,
		type ChartPoint
	} from '$lib/studio-gpu-chart';
	import type { GpuTimeline } from '$lib/studio-gpu-timeline';
	import type { MetricsRange } from '$lib/studio-metrics-range';
	import StudioGpuMetricLineChart from '$lib/components/studio-gpu-metric-line-chart.svelte';
	import StudioMetricsRangePicker from '$lib/components/studio-metrics-range-picker.svelte';
	import * as Card from '$lib/components/ui/card';

	let {
		timeline,
		vramAvailable = false,
		range = $bindable<MetricsRange>('5m'),
		live = false
	}: {
		timeline: GpuTimeline;
		vramAvailable?: boolean;
		range?: MetricsRange;
		live?: boolean;
	} = $props();

	const metricLanes = $derived(timeline.lanes.filter((lane) => lane.kind === 'metric'));
	const modelLanes = $derived(timeline.lanes.filter((lane) => lane.kind === 'model'));

	const swimWidth = 720;
	const swimMargin = CHART_MARGINS;
	const labelW = 108;
	const plotLeft = swimMargin.left + labelW;
	const plotWidth = swimWidth - swimMargin.left - swimMargin.right - labelW;
	const rowH = 36;
	const swimPlotTop = 12;
	const swimHeight = $derived(
		modelLanes.length > 0 ? swimPlotTop + modelLanes.length * rowH + swimMargin.bottom + 8 : 0
	);
	const xTicks = $derived(timeTicks(timeline.windowStartMs, timeline.windowEndMs, 6));
	const rangeCaption = $derived(
		formatRangeCaption(timeline.windowStartMs, timeline.windowEndMs, range)
	);

	function laneY(index: number): number {
		return swimPlotTop + index * rowH;
	}

	function blockWidth(startMs: number, endMs: number): number {
		return Math.max(
			3,
			xScale(endMs, timeline.windowStartMs, timeline.windowEndMs, plotLeft, plotWidth) -
				xScale(startMs, timeline.windowStartMs, timeline.windowEndMs, plotLeft, plotWidth)
		);
	}

	function rollingWindowForLane(laneId: string): number {
		return laneId === 'vram' ? 4 : 8;
	}
</script>

<div class="flex flex-col gap-4">
	<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
		<Card.Header class="border-border border-b px-5 py-4">
			<div class="flex flex-wrap items-center justify-between gap-3">
				<div>
					<Card.Title class="text-base">{t('app.metrics.gpu_timeline')}</Card.Title>
					<Card.Description class="text-sm">
						{t('app.metrics.gpu_timeline_window')}
					</Card.Description>
				</div>
				<div class="flex flex-wrap items-center gap-2">
					<StudioMetricsRangePicker bind:value={range} />
					{#if timeline.hardwareAvailable}
						<span class="bg-chart-2/15 text-chart-2 rounded-full px-2.5 py-1 text-xs font-medium">
							{t('app.metrics.live_hardware')}
						</span>
					{/if}
				</div>
			</div>
		</Card.Header>
		<Card.Content class="grid gap-5 p-5 lg:grid-cols-2">
			{#each metricLanes as lane (lane.id)}
				<StudioGpuMetricLineChart
					title={lane.label}
					seriesColor={lane.color}
					points={(lane.points ?? []) as ChartPoint[]}
					windowStartMs={timeline.windowStartMs}
					windowEndMs={timeline.windowEndMs}
					{range}
					{live}
					rollingWindow={rollingWindowForLane(lane.id)}
					yUnitLabel="%"
					emptyHint={lane.id === 'vram' && !vramAvailable
						? t('app.metrics.vram_unavailable')
						: undefined}
				/>
			{/each}
		</Card.Content>
	</Card.Root>

	{#if modelLanes.length > 0}
		<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
			<Card.Header class="border-border border-b px-5 py-4">
				<Card.Title class="text-base">{t('app.metrics.model_activity')}</Card.Title>
				<Card.Description class="text-sm">{t('app.metrics.model_activity_desc')}</Card.Description>
			</Card.Header>
			<Card.Content class="overflow-x-auto p-4">
				<svg
					viewBox="0 0 {swimWidth} {swimHeight}"
					class="h-auto w-full min-w-[36rem]"
					role="img"
					aria-label={t('app.metrics.model_activity')}
				>
					{#each modelLanes as lane, index (lane.id)}
						{@const y = laneY(index)}
						<rect
							x={plotLeft}
							{y}
							width={plotWidth}
							height={rowH - 4}
							rx="6"
							class={index % 2 === 0 ? 'fill-muted/25' : 'fill-muted/10'}
						/>
						<text
							x={swimMargin.left}
							y={y + rowH / 2 + 1}
							class="fill-muted-foreground font-mono text-[10px]"
						>
							{lane.label.length > 14 ? `${lane.label.slice(0, 13)}…` : lane.label}
						</text>
						<circle
							cx={swimMargin.left + labelW - 14}
							cy={y + rowH / 2 - 1}
							r="4"
							fill={lane.color}
						/>
						{#each lane.blocks ?? [] as block (block.id)}
							<rect
								x={xScale(
									block.startMs,
									timeline.windowStartMs,
									timeline.windowEndMs,
									plotLeft,
									plotWidth
								)}
								y={y + 7}
								width={blockWidth(block.startMs, block.endMs)}
								height={rowH - 18}
								rx="4"
								fill={lane.color}
								opacity="0.88"
							/>
						{/each}
					{/each}

					{#each xTicks as tick, index (index)}
						<text
							x={xScale(tick, timeline.windowStartMs, timeline.windowEndMs, plotLeft, plotWidth)}
							y={swimHeight - 6}
							text-anchor={index === 0 ? 'start' : index === xTicks.length - 1 ? 'end' : 'middle'}
							class="fill-muted-foreground font-mono text-[10px] tabular-nums"
						>
							{formatTimeTick(tick, range)}
						</text>
					{/each}
				</svg>

				<p class="text-muted-foreground mt-2 px-1 text-[10px] tabular-nums">{rangeCaption}</p>

				<div class="mt-3 flex flex-wrap gap-3">
					{#each modelLanes as lane (lane.id)}
						<span class="text-muted-foreground flex items-center gap-1.5 text-xs">
							<span class="inline-block size-2.5 rounded-sm" style={`background: ${lane.color}`}
							></span>
							<span class="font-mono">{lane.label}</span>
						</span>
					{/each}
				</div>
			</Card.Content>
		</Card.Root>
	{:else}
		<Card.Root class="p-5">
			<p class="text-muted-foreground text-sm">{t('app.metrics.gpu_timeline_empty')}</p>
		</Card.Root>
	{/if}

	<p class="text-muted-foreground px-1 text-xs leading-relaxed">
		{timeline.hardwareAvailable
			? t('app.metrics.gpu_timeline_hint_hw')
			: t('app.metrics.gpu_timeline_hint')}
	</p>
</div>
