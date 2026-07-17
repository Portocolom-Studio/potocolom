<script lang="ts">
	import { BarChart } from 'layerchart';
	import { formatMs, type ModelStats } from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';
	import * as Card from '$lib/components/ui/card';
	import * as Chart from '$lib/components/ui/chart';

	let { stats }: { stats: ModelStats[] } = $props();

	type Row = { model: string; gpuMs: number; wallMs: number };

	const rows = $derived(
		stats
			.filter((stat) => stat.avg_gpu_ms != null && stat.avg_wall_s != null)
			.map((stat) => ({
				model: stat.model_id,
				gpuMs: stat.avg_gpu_ms as number,
				wallMs: (stat.avg_wall_s as number) * 1000
			}))
			.sort((a, b) => a.gpuMs - b.gpuMs)
	);

	const chartConfig = {
		gpuMs: { label: t('app.metrics.col_gpu_avg'), color: 'var(--chart-1)' },
		wallMs: { label: t('app.metrics.col_wall'), color: 'var(--chart-3)' }
	} satisfies Chart.ChartConfig;

	const chartHeight = $derived(rows.length * 56 + 48);
</script>

<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
	<Card.Header class="border-border border-b px-5 py-4">
		<div class="flex flex-wrap items-center justify-between gap-3">
			<div>
				<Card.Title class="text-base">{t('app.metrics.benchmark_chart')}</Card.Title>
				<Card.Description class="text-sm">
					{t('app.metrics.benchmark_chart_desc')}
				</Card.Description>
			</div>
			<div class="flex items-center gap-4">
				{#each Object.values(chartConfig) as entry (entry.label)}
					<span class="text-muted-foreground flex items-center gap-1.5 text-xs">
						<span class="inline-block size-2.5" style={`background: ${entry.color}`}></span>
						{entry.label}
					</span>
				{/each}
			</div>
		</div>
	</Card.Header>
	<Card.Content class="p-5">
		<Chart.Container config={chartConfig} class="w-full" style={`height: ${chartHeight}px`}>
			<BarChart
				data={rows}
				orientation="horizontal"
				y={(d: Row) => d.model}
				bandPadding={0.3}
				padding={{ left: 124, bottom: 24 }}
				rule={false}
				seriesLayout="group"
				series={[
					{
						key: 'gpuMs',
						label: chartConfig.gpuMs.label,
						color: chartConfig.gpuMs.color
					},
					{
						key: 'wallMs',
						label: chartConfig.wallMs.label,
						color: chartConfig.wallMs.color
					}
				]}
				props={{
					bars: { motion: 'tween', 'stroke-width': 0 },
					xAxis: { format: (v: number) => formatMs(v) },
					yAxis: {
						format: (label: string) => (label.length > 14 ? `${label.slice(0, 13)}…` : label)
					},
					grid: { class: 'stroke-border/50' }
				}}
			>
				{#snippet tooltip()}
					<Chart.Tooltip labelFormatter={(label: string) => label} indicator="line" />
				{/snippet}
			</BarChart>
		</Chart.Container>
	</Card.Content>
</Card.Root>
