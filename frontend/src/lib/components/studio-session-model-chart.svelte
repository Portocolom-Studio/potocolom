<script lang="ts">
	import { BarChart } from 'layerchart';
	import { formatMs } from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';
	import type { ModelRuntimeRow } from '$lib/studio-session-metrics';
	import * as Card from '$lib/components/ui/card';
	import * as Chart from '$lib/components/ui/chart';

	let { rows }: { rows: ModelRuntimeRow[] } = $props();

	type DisplayRow = {
		key: string;
		modelId: string;
		count: number;
		avgGpuMs: number;
	};

	const displayRows = $derived.by((): DisplayRow[] => {
		if (rows.length <= 6) {
			return rows.map((row) => ({
				key: row.modelId,
				modelId: row.modelId,
				count: row.count,
				avgGpuMs: row.avgGpuMs
			}));
		}
		const head = rows.slice(0, 5);
		const tail = rows.slice(5);
		const tailCount = tail.reduce((sum, row) => sum + row.count, 0);
		const tailTotal = tail.reduce((sum, row) => sum + row.totalGpuMs, 0);
		return [
			...head.map((row) => ({
				key: row.modelId,
				modelId: row.modelId,
				count: row.count,
				avgGpuMs: row.avgGpuMs
			})),
			{
				key: '__other__',
				modelId: t('app.metrics.other_models'),
				count: tailCount,
				avgGpuMs: tailCount > 0 ? tailTotal / tailCount : 0
			}
		];
	});

	const chartConfig = {
		avgGpuMs: { label: 'GPU', color: 'var(--chart-1)' }
	} satisfies Chart.ChartConfig;

	const chartHeight = $derived(displayRows.length * 44 + 40);
</script>

<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
	<Card.Header class="border-border border-b px-5 py-4">
		<Card.Title class="text-base">{t('app.metrics.by_model')}</Card.Title>
		<Card.Description class="text-sm">{t('app.metrics.by_model_desc')}</Card.Description>
	</Card.Header>
	<Card.Content class="p-5">
		<Chart.Container config={chartConfig} class="w-full" style={`height: ${chartHeight}px`}>
			<BarChart
				data={displayRows}
				orientation="horizontal"
				y={(d: DisplayRow) => d.modelId}
				x={(d: DisplayRow) => d.avgGpuMs}
				bandPadding={0.35}
				padding={{ left: 132, bottom: 24 }}
				rule={false}
				series={[
					{
						key: 'avgGpuMs',
						label: t('app.metrics.col_gpu'),
						color: 'var(--chart-1)'
					}
				]}
				props={{
					bars: { motion: 'tween', 'stroke-width': 0 },
					xAxis: { format: (v: number) => formatMs(v) },
					yAxis: {
						format: (label: string) => (label.length > 16 ? `${label.slice(0, 15)}…` : label)
					},
					grid: { class: 'stroke-border/50' }
				}}
			>
				{#snippet tooltip()}
					<Chart.Tooltip labelFormatter={(label: string) => label} indicator="line" />
				{/snippet}
			</BarChart>
		</Chart.Container>
		<p class="text-muted-foreground mt-2 text-[11px] tabular-nums">
			{displayRows.reduce((sum, row) => sum + row.count, 0)}
			{t('app.metrics.samples')}
		</p>
	</Card.Content>
</Card.Root>
