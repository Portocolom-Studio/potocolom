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
		live = false,
		showModelActivity = true,
		metricEmptyHint,
		modelActivityEmptyHint,
		hintText
	}: {
		timeline: GpuTimeline;
		vramAvailable?: boolean;
		range?: MetricsRange;
		live?: boolean;
		showModelActivity?: boolean;
		metricEmptyHint?: string;
		modelActivityEmptyHint?: string;
		hintText?: string;
	} = $props();

	const metricLanes = $derived(timeline.lanes.filter((lane) => lane.kind === 'metric'));
	const modelLanes = $derived(timeline.lanes.filter((lane) => lane.kind === 'model'));
	const visibleMetricLanes = $derived(
		metricLanes.filter(
			(lane) =>
				lane.id !== 'vram' ||
				vramAvailable ||
				(lane.points?.length ?? 0) > 0 ||
				(lane.bands?.length ?? 0) > 0
		)
	);
	const vramHidden = $derived(visibleMetricLanes.length < metricLanes.length);

	let swimWidth = $state(720);
	const swimMargin = CHART_MARGINS;
	const plotLeft = swimMargin.left;
	const plotWidth = $derived(Math.max(1, swimWidth - swimMargin.left - swimMargin.right));
	const rowH = 64;
	const swimPlotTop = 8;
	const swimHeight = $derived(
		modelLanes.length > 0 ? swimPlotTop + modelLanes.length * rowH + swimMargin.bottom + 8 : 0
	);
	const xTicks = $derived(timeTicks(timeline.windowStartMs, timeline.windowEndMs, 3));
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
</script>

<div class="flex flex-col gap-4">
	<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
		<Card.Header class="border-border border-b px-5 py-4">
			<div class="flex flex-wrap items-center justify-between gap-3">
				<div>
					<Card.Title class="text-base">{t('app.metrics.gpu_timeline')}</Card.Title>
					<Card.Description class="text-sm tabular-nums">
						{rangeCaption}
					</Card.Description>
					{#if vramHidden}
						<p class="text-muted-foreground mt-1 text-xs">{t('app.metrics.vram_unavailable')}</p>
					{/if}
				</div>
				<div class="flex flex-wrap items-center gap-2">
					<StudioMetricsRangePicker bind:value={range} />
					{#if timeline.hardwareAvailable && live}
						<span class="bg-chart-2/15 text-chart-2 rounded-full px-2.5 py-1 text-xs font-medium">
							{t('app.metrics.live_hardware')}
						</span>
					{/if}
				</div>
			</div>
		</Card.Header>
		<Card.Content class="overflow-x-auto p-5">
			<div class="grid min-w-[28rem] gap-5">
				{#each visibleMetricLanes as lane (lane.id)}
					<StudioGpuMetricLineChart
						title={lane.label}
						seriesColor={lane.color}
						points={(lane.points ?? []) as ChartPoint[]}
						bands={lane.bands ?? []}
						windowStartMs={timeline.windowStartMs}
						windowEndMs={timeline.windowEndMs}
						{range}
						{live}
						emptyHint={metricEmptyHint ??
							(lane.id === 'vram' && !vramAvailable
								? t('app.metrics.vram_unavailable')
								: undefined)}
					/>
				{/each}
			</div>
		</Card.Content>
	</Card.Root>

	{#if showModelActivity && modelLanes.length > 0}
		<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
			<Card.Header class="border-border border-b px-5 py-4">
				<Card.Title class="text-base">{t('app.metrics.model_activity')}</Card.Title>
				<Card.Description class="text-sm">{t('app.metrics.model_activity_desc')}</Card.Description>
			</Card.Header>
			<Card.Content class="overflow-x-auto p-5">
				<div class="w-full min-w-[28rem]" bind:clientWidth={swimWidth}>
					<svg
						viewBox="0 0 {swimWidth} {swimHeight}"
						class="h-auto w-full"
						role="img"
						aria-label={t('app.metrics.model_activity')}
					>
						{#each modelLanes as lane, index (lane.id)}
							{@const y = laneY(index)}
							<rect
								x={plotLeft}
								y={y + 24}
								width={plotWidth}
								height="24"
								rx="6"
								class={index % 2 === 0 ? 'fill-muted/25' : 'fill-muted/10'}
							/>
							<text x="0" y={y + 12} class="fill-muted-foreground font-sans text-xs">
								{lane.label.length > 14 ? `${lane.label.slice(0, 11)}...` : lane.label}
							</text>
							{#each lane.blocks ?? [] as block (block.id)}
								<rect
									x={xScale(
										block.startMs,
										timeline.windowStartMs,
										timeline.windowEndMs,
										plotLeft,
										plotWidth
									)}
									y={y + 31}
									width={blockWidth(block.startMs, block.endMs)}
									height="10"
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
								class="fill-muted-foreground font-sans text-xs tabular-nums"
							>
								{formatTimeTick(tick, range)}
							</text>
						{/each}
					</svg>
				</div>

				{#if modelLanes.length > 1 || modelLanes.some((lane) => lane.label.length > 14)}
					<div class="mt-3 flex flex-wrap gap-3">
						{#each modelLanes as lane (lane.id)}
							<span
								class="text-muted-foreground flex max-w-full min-w-0 items-center gap-1.5 text-xs"
							>
								<span class="inline-block size-2.5 rounded-sm" style={`background: ${lane.color}`}
								></span>
								<span class="min-w-0 break-all">{lane.label}</span>
							</span>
						{/each}
					</div>
				{/if}
			</Card.Content>
		</Card.Root>
	{:else if showModelActivity}
		<Card.Root class="p-5">
			<p class="text-muted-foreground text-sm">
				{modelActivityEmptyHint ?? t('app.metrics.gpu_timeline_empty')}
			</p>
		</Card.Root>
	{/if}

	<p class="text-muted-foreground px-1 text-xs leading-relaxed">
		{hintText ??
			(timeline.hardwareAvailable
				? t('app.metrics.gpu_timeline_hint_hw')
				: t('app.metrics.gpu_timeline_hint'))}
	</p>
</div>
