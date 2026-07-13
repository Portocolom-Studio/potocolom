<script lang="ts">
	import {
		chartColor,
		categoryLineSeries,
		leaderboardRows,
		metricDisplay,
		metricValue,
		type BenchmarkReport,
		type MetricKey
	} from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import * as Card from '$lib/components/ui/card';
	import * as ToggleGroup from '$lib/components/ui/toggle-group';

	let { report }: { report: BenchmarkReport } = $props();

	let metric = $state<MetricKey>('gpu');

	const rows = $derived(leaderboardRows(report.model_stats));
	const maxMetric = $derived(Math.max(...rows.map((r) => metricValue(r, metric)), 1));
	const lineData = $derived(categoryLineSeries(report.results, report.models));
	const lineMax = $derived(
		Math.max(...lineData.series.flatMap((s) => s.points.map((p) => p.avg_gpu_ms)), 1)
	);
	const maxPair = $derived(
		Math.max(...rows.map((r) => Math.max(r.gpu_ms, r.wall_s * 1000)), 1)
	);

	const chartW = 360;
	const chartH = 220;
	const barH = 28;
	const barGap = 10;
	const barsH = $derived(rows.length * (barH + barGap) + 16);
	const groupBarW = 14;
	const groupGap = 6;

	const metrics: { key: MetricKey; label: string }[] = [
		{ key: 'gpu', label: 'GPU' },
		{ key: 'wall', label: 'Wall' },
		{ key: 'load', label: 'Load' }
	];

	function linePath(values: number[]): string {
		if (values.length === 0) return '';
		const step = chartW / Math.max(values.length - 1, 1);
		return values
			.map((v, i) => {
				const x = i * step;
				const y = chartH - (v / lineMax) * (chartH - 20) - 10;
				return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
			})
			.join(' ');
	}

	function heatColor(ms: number): string {
		const t = Math.min(1, ms / lineMax);
		return `color-mix(in oklch, var(--chart-1) ${Math.round(t * 85 + 10)}%, transparent)`;
	}
</script>

<div class="space-y-4">
	<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
		<Card.Header class="border-b px-5 py-4">
			<Card.Title class="text-base">{t('bench.leaderboard')}</Card.Title>
			<Card.Description class="text-sm">{t('bench.leaderboard_note')}</Card.Description>
		</Card.Header>
		<div class="overflow-x-auto">
			<table class="w-full min-w-[640px] text-left text-sm">
				<thead class="bg-muted/30 text-muted-foreground text-xs">
					<tr>
						<th class="w-10 px-4 py-2.5 font-medium">#</th>
						<th class="px-4 py-2.5 font-medium">{t('bench.col_model')}</th>
						<th class="min-w-[140px] px-4 py-2.5 font-medium">{t('bench.col_gpu_avg')}</th>
						<th class="px-4 py-2.5 font-medium tabular-nums">{t('bench.col_wall')}</th>
						<th class="px-4 py-2.5 font-medium tabular-nums">{t('bench.col_load')}</th>
					</tr>
				</thead>
				<tbody>
					{#each rows as row (row.model_id)}
						<tr class="border-t border-border/60">
							<td class="text-muted-foreground px-4 py-3 tabular-nums">{row.rank}</td>
							<td class="px-4 py-3">
								<span class="font-mono text-xs">{row.model_id}</span>
								{#if row.reference}
									<Badge class="ms-2" variant="secondary">{t('bench.reference_badge')}</Badge>
								{/if}
							</td>
							<td class="px-4 py-3">
								<div class="flex items-center gap-3">
									<div class="bg-muted/50 h-2 min-w-[88px] flex-1 overflow-hidden rounded-full">
										<div
											class="h-full rounded-full"
											style:width="{row.gpu_ratio * 100}%"
											style:background={chartColor(row.rank - 1)}
										></div>
									</div>
									<span class="w-14 shrink-0 text-end font-mono text-xs tabular-nums"
										>{row.gpu_display}</span
									>
								</div>
							</td>
							<td class="px-4 py-3 font-mono text-xs tabular-nums">{row.wall_display}</td>
							<td class="px-4 py-3 font-mono text-xs tabular-nums">{row.load_display}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</Card.Root>

	<div class="grid gap-4 lg:grid-cols-2">
		<Card.Root class="[--card-spacing:--spacing(5)]">
			<Card.Header class="gap-3">
				<div class="flex flex-wrap items-center justify-between gap-3">
					<div>
						<Card.Title class="text-base">{t('bench.chart_bars')}</Card.Title>
						<Card.Description class="text-sm">{t('bench.chart_bars_desc')}</Card.Description>
					</div>
					<ToggleGroup.Root type="single" bind:value={metric} class="rounded-full">
						{#each metrics as m (m.key)}
							<ToggleGroup.Item value={m.key} class="px-3 text-xs">{m.label}</ToggleGroup.Item>
						{/each}
					</ToggleGroup.Root>
				</div>
			</Card.Header>
			<Card.Content>
				<svg
					viewBox="0 0 {chartW + 120} {barsH}"
					class="w-full max-h-[280px]"
					role="img"
					aria-label={t('bench.chart_bars')}
				>
					{#each rows as row, i (row.model_id)}
						{@const y = i * (barH + barGap) + 8}
						{@const val = metricValue(row, metric)}
						{@const w =
							metric === 'load'
								? (Math.log10(Math.max(val, 1)) / Math.log10(Math.max(maxMetric, 1))) * chartW
								: (val / maxMetric) * chartW}
						<text x="0" y={y + barH * 0.72} class="fill-muted-foreground text-[10px]">
							{row.model_id.length > 14 ? `${row.model_id.slice(0, 13)}…` : row.model_id}
						</text>
						<rect
							x="108"
							{y}
							width={Math.max(2, w)}
							height={barH}
							rx="6"
							fill={chartColor(i)}
							opacity="0.9"
						/>
						<text
							x={108 + Math.max(w, 2) + 6}
							y={y + barH * 0.72}
							class="fill-foreground text-[10px] tabular-nums"
						>
							{metricDisplay(row, metric)}
						</text>
					{/each}
				</svg>
			</Card.Content>
		</Card.Root>

		<Card.Root class="[--card-spacing:--spacing(5)]">
			<Card.Header class="gap-1">
				<Card.Title class="text-base">{t('bench.chart_grouped')}</Card.Title>
				<Card.Description class="text-sm">{t('bench.chart_grouped_desc')}</Card.Description>
				<div class="flex gap-4 pt-1 text-xs">
					<span class="flex items-center gap-1.5"
						><span class="bg-chart-1 inline-block h-2 w-3 rounded-sm"></span> GPU</span
					>
					<span class="flex items-center gap-1.5"
						><span class="bg-chart-3 inline-block h-2 w-3 rounded-sm"></span> Wall</span
					>
				</div>
			</Card.Header>
			<Card.Content>
				<svg
					viewBox="0 0 {chartW + 120} {barsH}"
					class="w-full max-h-[280px]"
					role="img"
					aria-label={t('bench.chart_grouped')}
				>
					{#each rows as row, i (row.model_id)}
						{@const y = i * (barH + barGap) + 8}
						{@const gpuW = (row.gpu_ms / maxPair) * chartW}
						{@const wallW = (row.wall_s * 1000) / maxPair * chartW}
						<text x="0" y={y + barH * 0.72} class="fill-muted-foreground text-[10px]">
							{row.model_id.length > 14 ? `${row.model_id.slice(0, 13)}…` : row.model_id}
						</text>
						<rect
							x="108"
							{y}
							width={Math.max(2, gpuW)}
							height={groupBarW}
							rx="4"
							class="fill-chart-1"
						/>
						<rect
							x="108"
							y={y + groupBarW + groupGap}
							width={Math.max(2, wallW)}
							height={groupBarW}
							rx="4"
							fill={chartColor(i + 2)}
						/>
					{/each}
				</svg>
			</Card.Content>
		</Card.Root>
	</div>

	<div class="grid gap-4 lg:grid-cols-5">
		<Card.Root class="lg:col-span-3 [--card-spacing:--spacing(5)]">
			<Card.Header class="gap-1">
				<Card.Title class="text-base">{t('bench.chart_line')}</Card.Title>
				<Card.Description class="text-sm">{t('bench.chart_line_desc')}</Card.Description>
			</Card.Header>
			<Card.Content>
				<svg
					viewBox="0 0 {chartW} {chartH + 36}"
					class="w-full"
					role="img"
					aria-label={t('bench.chart_line')}
				>
					{#each [0, 0.25, 0.5, 0.75, 1] as tick (tick)}
						<line
							x1="0"
							y1={chartH - tick * (chartH - 20) - 10}
							x2={chartW}
							y2={chartH - tick * (chartH - 20) - 10}
							class="stroke-border/50"
							stroke-width="1"
							stroke-dasharray="4 4"
						/>
					{/each}
					{#each lineData.series as series, si (series.model_id)}
						<path
							d={linePath(series.points.map((p) => p.avg_gpu_ms))}
							fill="none"
							stroke={series.color}
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
						/>
						{#each series.points as point, pi (point.category)}
							<circle
								cx={(pi / Math.max(lineData.categories.length - 1, 1)) * chartW}
								cy={chartH - (point.avg_gpu_ms / lineMax) * (chartH - 20) - 10}
								r="3"
								fill={series.color}
							>
								<title>{series.model_id} · {point.category}: {Math.round(point.avg_gpu_ms)} ms</title>
							</circle>
						{/each}
					{/each}
					{#each lineData.categories as cat, i (cat)}
						<text
							x={(i / Math.max(lineData.categories.length - 1, 1)) * chartW}
							y={chartH + 22}
							text-anchor="middle"
							class="fill-muted-foreground text-[9px]"
						>
							{cat.slice(0, 5)}
						</text>
					{/each}
				</svg>
				<div class="mt-3 flex flex-wrap gap-x-4 gap-y-1">
					{#each lineData.series as series, i (series.model_id)}
						<span class="flex items-center gap-1.5 text-[10px]">
							<span
								class="inline-block h-2 w-3 rounded-sm"
								style:background={series.color}
							></span>
							<span class="font-mono">{series.model_id}</span>
						</span>
					{/each}
				</div>
			</Card.Content>
		</Card.Root>

		<Card.Root class="lg:col-span-2 [--card-spacing:--spacing(5)]">
			<Card.Header class="gap-1">
				<Card.Title class="text-base">{t('bench.chart_heatmap')}</Card.Title>
				<Card.Description class="text-sm">{t('bench.chart_heatmap_desc')}</Card.Description>
			</Card.Header>
			<Card.Content>
				<div class="overflow-x-auto">
					<table class="w-full border-separate border-spacing-1 text-[10px]">
						<thead>
							<tr>
								<th class="text-muted-foreground px-1 py-1 text-start font-medium"></th>
								{#each report.models as modelId (modelId)}
									<th class="text-muted-foreground px-1 py-1 text-center font-mono font-medium">
										{modelId.split('-')[0]}
									</th>
								{/each}
							</tr>
						</thead>
						<tbody>
							{#each lineData.categories as category, ci (category)}
								<tr>
									<td class="text-muted-foreground px-1 py-1 capitalize">{category.slice(0, 7)}</td>
									{#each report.models as modelId, mi (modelId)}
										{@const ms =
											lineData.series
												.find((s) => s.model_id === modelId)
												?.points.find((p) => p.category === category)?.avg_gpu_ms ?? 0}
										<td class="p-0">
											<div
												class="flex h-7 min-w-[2rem] items-center justify-center rounded-md font-mono tabular-nums"
												style:background={heatColor(ms)}
												title="{modelId} · {category}: {Math.round(ms)} ms"
											>
												{ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}`}
											</div>
										</td>
									{/each}
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</Card.Content>
		</Card.Root>
	</div>
</div>
