<script lang="ts">
	import BenchmarkCategoryRadar from '$lib/components/benchmark-category-radar.svelte';
	import {
		chartColor,
		categoryLineSeries,
		formatMs,
		formatMsHeat,
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
	const heatMax = $derived(
		Math.max(
			...lineData.series.flatMap((series) => series.points.map((point) => point.avg_gpu_ms)),
			1
		)
	);
	const heatMin = $derived(
		Math.min(
			...lineData.series.flatMap((series) =>
				series.points.map((point) => point.avg_gpu_ms).filter((ms) => ms > 0)
			),
			heatMax
		)
	);
	const maxPair = $derived(
		Math.max(...rows.map((r) => Math.max(r.gpu_ms, r.wall_s * 1000)), 1)
	);

	const chartW = 360;
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

	function heatCell(ms: number): { bar: string; track: string } {
		const span = Math.max(heatMax - heatMin, 1);
		const t = Math.min(1, Math.max(0, (ms - heatMin) / span));
		const bucket = Math.min(4, Math.floor(t * 5));
		const colors = [
			'var(--chart-4)',
			'var(--chart-2)',
			'var(--chart-5)',
			'var(--chart-3)',
			'var(--chart-1)'
		] as const;
		return {
			bar: colors[bucket],
			track: `color-mix(in oklch, ${colors[bucket]} 18%, var(--muted))`
		};
	}

	function shortModel(modelId: string): string {
		return modelId.length > 12 ? `${modelId.slice(0, 11)}…` : modelId;
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

	<Card.Root class="[--card-spacing:--spacing(5)]">
		<Card.Header class="gap-1">
			<Card.Title class="text-base">{t('bench.chart_category')}</Card.Title>
			<Card.Description class="text-sm">{t('bench.chart_category_desc')}</Card.Description>
		</Card.Header>
		<Card.Content>
			<BenchmarkCategoryRadar categories={lineData.categories} series={lineData.series} />
		</Card.Content>
	</Card.Root>

	<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
		<Card.Header class="border-b px-5 py-4">
			<Card.Title class="text-base">{t('bench.chart_heatmap')}</Card.Title>
			<Card.Description class="text-sm">{t('bench.chart_heatmap_desc')}</Card.Description>
			<div class="mt-3 flex flex-wrap items-center gap-3 text-xs">
				<span class="text-muted-foreground">{t('bench.heatmap_fast')}</span>
				<div class="flex h-3 w-40 overflow-hidden rounded-full border">
					{#each [0, 1, 2, 3, 4] as bucket (bucket)}
						<div
							class="h-full flex-1"
							style:background={heatCell(heatMin + ((heatMax - heatMin) * bucket) / 4).bar}
						></div>
					{/each}
				</div>
				<span class="text-muted-foreground">{t('bench.heatmap_slow')}</span>
				<span class="text-muted-foreground ms-auto font-mono tabular-nums">
					{formatMs(heatMin)} - {formatMs(heatMax)}
				</span>
			</div>
		</Card.Header>
		<Card.Content class="p-4">
			<div class="overflow-x-auto">
				<table class="w-full min-w-[640px] border-separate border-spacing-1.5 text-xs">
					<thead>
						<tr>
							<th
								class="text-muted-foreground sticky left-0 z-10 bg-card px-3 py-2 text-start text-sm font-medium"
							>
								{t('bench.heatmap_category')}
							</th>
							{#each report.models as modelId (modelId)}
								<th
									class="text-muted-foreground px-2 py-2 text-center font-mono text-[11px] font-medium"
									title={modelId}
								>
									{shortModel(modelId)}
								</th>
							{/each}
						</tr>
					</thead>
					<tbody>
						{#each lineData.categories as category (category)}
							<tr>
								<td
									class="text-foreground sticky left-0 z-10 bg-card px-3 py-2 text-sm font-medium capitalize"
								>
									{category}
								</td>
								{#each report.models as modelId (modelId)}
									{@const ms =
										lineData.series
											.find((series) => series.model_id === modelId)
											?.points.find((point) => point.category === category)?.avg_gpu_ms ?? 0}
									{@const heat = heatCell(ms)}
									{@const label = formatMsHeat(ms)}
									<td class="p-0">
										<div
											class="bg-card flex min-w-[3.5rem] flex-col items-center rounded-lg border px-2 py-2.5"
											style:background={heat.track}
											title="{modelId} · {category}: {formatMs(ms)}"
										>
											<div
												class="mb-2 h-1 w-full rounded-full"
												style:background={heat.bar}
											></div>
											<span class="text-foreground text-base leading-none font-semibold tabular-nums">
												{label.value}
											</span>
											{#if label.unit !== ''}
												<span class="text-muted-foreground mt-1 text-[10px] leading-none uppercase">
													{label.unit}
												</span>
											{/if}
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
