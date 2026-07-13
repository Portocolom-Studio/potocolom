<script lang="ts">
	import BenchmarkCategoryRadar from '$lib/components/benchmark-category-radar.svelte';
	import {
		chartColor,
		categoryLineSeries,
		formatMs,
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
	const labelW = 210;
	const barStart = labelW + 12;
	const svgW = barStart + chartW + 104;
	const rowH = 36;
	const barGap = 12;
	const metricBarH = 30;
	const barsH = $derived(rows.length * (rowH + barGap) + 16);
	const groupBarW = 14;
	const groupGap = 6;

	const metrics: { key: MetricKey; label: string }[] = [
		{ key: 'gpu', label: 'GPU' },
		{ key: 'wall', label: 'Wall' },
		{ key: 'load', label: 'Load' }
	];

	const heatFast = 'oklch(0.72 0.11 205)';
	const heatSlow = 'var(--card)';

	/** 0 = fastest, 1 = slowest. Log-scaled so 330 ms vs 1.5 s reads clearly. */
	function heatTone(ms: number): number {
		if (ms <= 0) return 1;
		const lo = Math.max(heatMin, 1);
		const hi = Math.max(heatMax, lo * 1.01);
		if (hi <= lo) return 0;
		const logT =
			(Math.log(Math.max(ms, lo)) - Math.log(lo)) / (Math.log(hi) - Math.log(lo));
		return Math.min(1, Math.max(0, logT));
	}

	function heatLegendMs(fraction: number): number {
		const lo = Math.max(heatMin, 1);
		const hi = Math.max(heatMax, lo * 1.01);
		return Math.exp(Math.log(lo) + fraction * (Math.log(hi) - Math.log(lo)));
	}

	/** Sequential fill: faster (lower ms) → lighter blue tint, slower → near card. */
	function heatFill(ms: number): string {
		if (ms <= 0) return heatSlow;
		const slow = heatTone(ms);
		const fast = 1 - Math.pow(slow, 0.82);
		const mix = 2 + fast * 66;
		return `color-mix(in oklch, ${heatFast} ${mix}%, ${heatSlow})`;
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
					viewBox="0 0 {svgW} {barsH}"
					class="w-full max-h-[360px]"
					role="img"
					aria-label={t('bench.chart_bars')}
				>
					{#each rows as row, i (row.model_id)}
						{@const y = i * (rowH + barGap) + 8}
						{@const val = metricValue(row, metric)}
						{@const w =
							metric === 'load'
								? (Math.log10(Math.max(val, 1)) / Math.log10(Math.max(maxMetric, 1))) * chartW
								: (val / maxMetric) * chartW}
						<text
							x="0"
							y={y + rowH / 2}
							dominant-baseline="middle"
							class="fill-muted-foreground font-mono text-[20px]"
						>
							{row.model_id}
						</text>
						<rect
							x={barStart}
							y={y + (rowH - metricBarH) / 2}
							width={Math.max(2, w)}
							height={metricBarH}
							rx="6"
							fill={chartColor(i)}
							opacity="0.9"
						/>
						<text
							x={barStart + Math.max(w, 2) + 8}
							y={y + rowH / 2}
							dominant-baseline="middle"
							class="fill-foreground font-mono text-[20px] tabular-nums"
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
					viewBox="0 0 {svgW} {barsH}"
					class="w-full max-h-[360px]"
					role="img"
					aria-label={t('bench.chart_grouped')}
				>
					{#each rows as row, i (row.model_id)}
						{@const y = i * (rowH + barGap) + 8}
						{@const gpuW = (row.gpu_ms / maxPair) * chartW}
						{@const wallW = (row.wall_s * 1000) / maxPair * chartW}
						{@const groupH = groupBarW * 2 + groupGap}
						{@const groupTop = y + (rowH - groupH) / 2}
						<text
							x="0"
							y={y + rowH / 2}
							dominant-baseline="middle"
							class="fill-muted-foreground font-mono text-[20px]"
						>
							{row.model_id}
						</text>
						<rect
							x={barStart}
							y={groupTop}
							width={Math.max(2, gpuW)}
							height={groupBarW}
							rx="4"
							class="fill-chart-1"
						/>
						<rect
							x={barStart}
							y={groupTop + groupBarW + groupGap}
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

	<Card.Root class="gap-2 [--card-spacing:--spacing(5)]">
		<Card.Header class="gap-1 pb-0">
			<Card.Title class="text-base">{t('bench.chart_category')}</Card.Title>
			<Card.Description class="text-sm">{t('bench.chart_category_desc')}</Card.Description>
		</Card.Header>
		<Card.Content class="pt-0">
			<BenchmarkCategoryRadar categories={lineData.categories} series={lineData.series} />
		</Card.Content>
	</Card.Root>

	<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
		<Card.Header class="border-b px-5 py-4">
			<Card.Title class="text-base">{t('bench.chart_heatmap')}</Card.Title>
			<Card.Description class="text-sm">{t('bench.chart_heatmap_desc')}</Card.Description>
			<div class="mt-3 flex flex-wrap items-center gap-3 text-xs">
				<span class="text-muted-foreground">{t('bench.heatmap_fast')}</span>
				<div class="border-border flex h-2 w-44 border">
					{#each [0, 1, 2, 3, 4] as bucket (bucket)}
						<div
							class="h-full flex-1"
							style:background={heatFill(heatLegendMs(bucket / 4))}
						></div>
					{/each}
				</div>
				<span class="text-muted-foreground">{t('bench.heatmap_slow')}</span>
				<span class="text-muted-foreground ms-auto font-mono text-[11px] tabular-nums">
					{formatMs(heatMin)} - {formatMs(heatMax)}
				</span>
			</div>
		</Card.Header>
		<Card.Content class="p-0">
			<div class="overflow-x-auto">
				<table class="w-full min-w-[640px] border-collapse text-xs">
					<thead>
						<tr class="border-border border-b">
							<th
								class="text-muted-foreground border-border sticky left-0 z-10 border-r bg-card px-4 py-2.5 text-start text-[11px] font-medium tracking-wide uppercase"
							>
								{t('bench.heatmap_category')}
							</th>
							{#each report.models as modelId (modelId)}
								<th
									class="text-muted-foreground border-border border-l px-2 py-2.5 text-center font-mono text-[11px] font-normal"
									title={modelId}
								>
									{shortModel(modelId)}
								</th>
							{/each}
						</tr>
					</thead>
					<tbody>
						{#each lineData.categories as category (category)}
							<tr class="border-border/60 border-b last:border-b-0">
								<td
									class="text-foreground border-border sticky left-0 z-10 border-r bg-card px-4 py-2.5 text-sm font-medium capitalize"
								>
									{category}
								</td>
								{#each report.models as modelId (modelId)}
									{@const ms =
										lineData.series
											.find((series) => series.model_id === modelId)
											?.points.find((point) => point.category === category)?.avg_gpu_ms ?? 0}
									<td
										class="border-border/60 border-l px-3 py-2.5 text-center font-mono tabular-nums"
										style:background={heatFill(ms)}
										title="{modelId} · {category}: {formatMs(ms)}"
									>
										{formatMs(ms)}
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
