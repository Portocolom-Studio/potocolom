<script lang="ts">
	import { LineChart } from 'layerchart';
	import { chartColor, formatMs, type BenchmarkReport } from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';
	import * as Card from '$lib/components/ui/card';
	import * as Chart from '$lib/components/ui/chart';

	let { report }: { report: BenchmarkReport } = $props();

	type Point = { t: number; gpuMs: number };

	/* The suite runs one model at a time, so cumulative wall time (plus the
	   one-off model loads) reconstructs an elapsed-time axis. The published
	   report carries no hardware GPU/VRAM samples - see issue #107. */
	const byModel = $derived.by(() => {
		let elapsedS = 0;
		let lastModel: string | null = null;
		const map = new Map<string, Point[]>();
		for (const result of report.results) {
			// model_load_ms is stamped on every row; the load is only paid
			// once, when the suite switches to the next model
			if (result.model_id !== lastModel) {
				elapsedS += (result.model_load_ms ?? 0) / 1000;
				lastModel = result.model_id;
			}
			elapsedS += result.wall_s ?? 0;
			if (result.state !== 'succeeded' || result.gpu_ms == null) continue;
			const points = map.get(result.model_id) ?? [];
			points.push({ t: elapsedS, gpuMs: result.gpu_ms });
			map.set(result.model_id, points);
		}
		return map;
	});

	const series = $derived(
		report.model_stats
			.map((stat) => stat.model_id)
			.filter((modelId) => byModel.has(modelId))
			.map((modelId, index) => ({
				key: modelId,
				label: modelId,
				color: chartColor(index),
				data: byModel.get(modelId)
			}))
	);

	const chartConfig = $derived(
		Object.fromEntries(
			series.map((entry) => [entry.key, { label: entry.label, color: entry.color }])
		) satisfies Chart.ChartConfig
	);

	function formatElapsed(seconds: number): string {
		if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} h`;
		return `${Math.round(seconds / 60)} min`;
	}
</script>

<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
	<Card.Header class="border-border border-b px-5 py-4">
		<div class="flex flex-wrap items-center justify-between gap-3">
			<div>
				<Card.Title class="text-base">{t('app.metrics.benchmark_timeline')}</Card.Title>
				<Card.Description class="text-sm">
					{t('app.metrics.benchmark_timeline_desc')}
				</Card.Description>
			</div>
			<div class="flex flex-wrap items-center gap-4">
				{#each series as entry (entry.key)}
					<span class="text-muted-foreground flex items-center gap-1.5 text-xs">
						<span class="inline-block size-2.5" style={`background: ${entry.color}`}></span>
						{entry.label}
					</span>
				{/each}
			</div>
		</div>
	</Card.Header>
	<Card.Content class="p-5">
		<Chart.Container config={chartConfig} class="h-[260px] w-full">
			<LineChart
				data={[]}
				x={(d: Point) => d.t}
				y={(d: Point) => d.gpuMs}
				{series}
				rule={false}
				points={false}
				props={{
					spline: { motion: 'tween', class: 'stroke-[1.5px]' },
					xAxis: { format: formatElapsed, ticks: 6 },
					yAxis: { format: (v: number) => formatMs(v) },
					grid: { class: 'stroke-border/50' }
				}}
			>
				{#snippet tooltip()}
					<Chart.Tooltip
						labelFormatter={(seconds: number) => formatElapsed(seconds)}
						indicator="line"
					/>
				{/snippet}
			</LineChart>
		</Chart.Container>
	</Card.Content>
</Card.Root>
