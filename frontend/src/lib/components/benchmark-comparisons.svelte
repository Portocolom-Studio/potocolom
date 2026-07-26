<script lang="ts">
	import {
		categoryLineSeries,
		formatMs,
		formatSeconds,
		leaderboardRows,
		type BenchmarkReport
	} from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';

	let { report }: { report: BenchmarkReport } = $props();

	const rows = $derived(leaderboardRows(report.model_stats));
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
	const maxPair = $derived(Math.max(...rows.map((r) => Math.max(r.gpu_ms, r.wall_s * 1000)), 1));

	const chartW = 360;
	const labelW = 210;
	const barStart = labelW + 12;
	const svgW = barStart + chartW + 104;
	const rowH = 36;
	const barGap = 12;
	const barsH = $derived(rows.length * (rowH + barGap) + 16);
	const groupBarW = 14;
	const groupGap = 6;

	const heatFast = 'oklch(0.72 0.11 205)';
	const heatSlow = 'var(--heat-base)';

	function statsFor(modelId: string) {
		return report.model_stats.find((row) => row.model_id === modelId);
	}

	/** 0 = fastest, 1 = slowest. Log-scaled so 330 ms vs 1.5 s reads clearly. */
	function heatTone(ms: number): number {
		if (ms <= 0) return 1;
		const lo = Math.max(heatMin, 1);
		const hi = Math.max(heatMax, lo * 1.01);
		if (hi <= lo) return 0;
		const logT = (Math.log(Math.max(ms, lo)) - Math.log(lo)) / (Math.log(hi) - Math.log(lo));
		return Math.min(1, Math.max(0, logT));
	}

	function heatLegendMs(fraction: number): number {
		const lo = Math.max(heatMin, 1);
		const hi = Math.max(heatMax, lo * 1.01);
		return Math.exp(Math.log(lo) + fraction * (Math.log(hi) - Math.log(lo)));
	}

	/** Sequential fill: faster (lower ms) takes a stronger tint, slower fades into the panel. */
	function heatFill(ms: number): string {
		if (ms <= 0) return heatSlow;
		const slow = heatTone(ms);
		const fast = 1 - Math.pow(slow, 0.82);
		const mix = 2 + fast * 66;
		return `color-mix(in oklch, ${heatFast} ${mix}%, ${heatSlow})`;
	}

	function shortModel(modelId: string): string {
		return modelId.length > 12 ? `${modelId.slice(0, 11)}...` : modelId;
	}
</script>

<div class="stack">
	<section class="card">
		<header>
			<h3>{t('bench.leaderboard')}</h3>
			<p>{t('bench.leaderboard_note')}</p>
		</header>
		<div class="scroll">
			<table class="lead">
				<thead>
					<tr>
						<th scope="col" class="rank">#</th>
						<th scope="col">{t('bench.col_model')}</th>
						<th scope="col" class="wide">{t('bench.col_gpu_avg')}</th>
						<th scope="col">{t('bench.col_gpu_med')}</th>
						<th scope="col">{t('bench.col_wall')}</th>
						<th scope="col">{t('bench.col_load')}</th>
						<th scope="col">{t('bench.col_ok')}</th>
					</tr>
				</thead>
				<tbody>
					{#each rows as row (row.model_id)}
						{@const stats = statsFor(row.model_id)}
						<tr>
							<td class="num quiet">{row.rank}</td>
							<td>
								<span class="mono">{row.model_id}</span>
								{#if row.reference}
									<span class="tag">{t('bench.reference_badge')}</span>
								{/if}
							</td>
							<td>
								<div class="meter">
									<div class="track">
										<div class="fill" style:width="{row.gpu_ratio * 100}%"></div>
									</div>
									<span class="mono num value">{row.gpu_display}</span>
								</div>
							</td>
							<td class="mono num">
								{stats?.median_gpu_ms ? formatMs(stats.median_gpu_ms) : '-'}
							</td>
							<td class="mono num">{row.wall_display}</td>
							<td class="mono num">{row.load_display}</td>
							<td class="mono num">
								{stats?.succeeded ?? 0}
								{#if stats?.failed}
									<span class="quiet">/ {stats.failed} {t('bench.failed')}</span>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>

	<section class="card">
		<header>
			<h3>{t('bench.chart_grouped')}</h3>
			<p>{t('bench.chart_grouped_desc')}</p>
			<div class="legend">
				<span><i class="swatch gpu"></i> GPU</span>
				<span><i class="swatch wall"></i> Wall</span>
			</div>
		</header>
		<div class="pad">
			<svg
				viewBox="0 0 {svgW} {barsH}"
				class="chart"
				role="img"
				aria-label={t('bench.chart_grouped')}
			>
				{#each rows as row, i (row.model_id)}
					{@const y = i * (rowH + barGap) + 8}
					{@const gpuW = (row.gpu_ms / maxPair) * chartW}
					{@const wallW = ((row.wall_s * 1000) / maxPair) * chartW}
					{@const groupH = groupBarW * 2 + groupGap}
					{@const groupTop = y + (rowH - groupH) / 2}
					<text x="0" y={y + rowH / 2} dominant-baseline="middle" class="tick">
						{row.model_id}
					</text>
					<rect
						x={barStart}
						y={groupTop}
						width={Math.max(2, gpuW)}
						height={groupBarW}
						rx="4"
						class="bar-gpu"
					/>
					<rect
						x={barStart}
						y={groupTop + groupBarW + groupGap}
						width={Math.max(2, wallW)}
						height={groupBarW}
						rx="4"
						class="bar-wall"
					/>
					<text
						x={barStart + Math.max(gpuW, wallW) + 10}
						y={y + rowH / 2}
						dominant-baseline="middle"
						class="tick value"
					>
						{formatSeconds(row.wall_s)}
					</text>
				{/each}
			</svg>
		</div>
	</section>

	<section class="card">
		<header>
			<h3>{t('bench.chart_heatmap')}</h3>
			<p>{t('bench.chart_heatmap_desc')}</p>
			<div class="legend">
				<span>{t('bench.heatmap_fast')}</span>
				<div class="ramp">
					{#each [0, 1, 2, 3, 4] as bucket (bucket)}
						<div style:background={heatFill(heatLegendMs(bucket / 4))}></div>
					{/each}
				</div>
				<span>{t('bench.heatmap_slow')}</span>
				<span class="mono num range">{formatMs(heatMin)} - {formatMs(heatMax)}</span>
			</div>
		</header>
		<div class="scroll">
			<table class="heat">
				<thead>
					<tr>
						<th scope="col" class="sticky">{t('bench.heatmap_category')}</th>
						{#each report.models as modelId (modelId)}
							<th scope="col" class="mono" title={modelId}>{shortModel(modelId)}</th>
						{/each}
					</tr>
				</thead>
				<tbody>
					{#each lineData.categories as category (category)}
						<tr>
							<th scope="row" class="sticky">{category}</th>
							{#each report.models as modelId (modelId)}
								{@const ms =
									lineData.series
										.find((series) => series.model_id === modelId)
										?.points.find((point) => point.category === category)?.avg_gpu_ms ?? 0}
								<td
									class="mono num cell"
									style:background={heatFill(ms)}
									title="{modelId} {category}: {formatMs(ms)}"
								>
									{formatMs(ms)}
								</td>
							{/each}
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
</div>

<style>
	/* Hallmark - the benchmark charts on the latent tokens, so they belong to the page. */
	.stack {
		--heat-base: oklch(0.13 0.018 265);
		display: grid;
		gap: 1rem;
		min-width: 0;
	}

	:global(:root[data-krea-mode='light']) .stack {
		--heat-base: oklch(0.96 0.004 255);
	}

	.card {
		min-width: 0;
		overflow: hidden;
		border: 1px solid var(--k-line);
		border-radius: 0.9rem;
		background: var(--k-panel);
	}

	header {
		display: grid;
		gap: 0.35rem;
		padding: 1rem 1.1rem;
		border-block-end: 1px solid var(--k-line);
	}

	h3 {
		margin: 0;
		font-size: 1rem;
		font-weight: 700;
		letter-spacing: -0.02em;
	}

	header p {
		margin: 0;
		max-width: 68ch;
		color: var(--k-muted);
		font-size: 0.85rem;
		line-height: 1.5;
	}

	.legend {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.85rem;
		padding-block-start: 0.35rem;
		color: var(--k-muted);
		font-size: 0.76rem;
	}

	.legend span {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
	}

	.swatch {
		width: 0.85rem;
		height: 0.5rem;
		border-radius: 0.15rem;
	}

	.swatch.gpu {
		background: var(--k-accent);
	}

	.swatch.wall {
		background: oklch(0.72 0.11 205);
	}

	.ramp {
		display: flex;
		width: 11rem;
		height: 0.5rem;
		border: 1px solid var(--k-line);
	}

	.ramp div {
		flex: 1;
	}

	.range {
		margin-inline-start: auto;
	}

	.pad {
		padding: 1rem 1.1rem;
	}

	.scroll {
		min-width: 0;
		overflow-x: auto;
	}

	/* Tables ---------------------------------------------------------------- */
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}

	.lead {
		min-width: 48rem;
	}

	.heat {
		min-width: 40rem;
	}

	th,
	td {
		padding: 0.6rem 0.85rem;
		text-align: start;
		white-space: nowrap;
	}

	thead th {
		border-block-end: 1px solid var(--k-line);
		color: var(--k-muted);
		font-size: 0.74rem;
		font-weight: 500;
	}

	tbody tr {
		border-block-start: 1px solid var(--k-line);
	}

	td {
		color: var(--k-ink);
	}

	.rank {
		width: 2.5rem;
	}

	.wide {
		min-width: 12rem;
	}

	.mono {
		font-family: var(--k-mono);
		font-size: 0.78rem;
	}

	.num {
		font-variant-numeric: tabular-nums;
	}

	.quiet {
		color: var(--k-muted);
	}

	.tag {
		margin-inline-start: 0.4rem;
		padding: 0.1rem 0.45rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		color: var(--k-muted);
		font-size: 0.68rem;
	}

	.meter {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}

	.track {
		flex: 1;
		min-width: 5.5rem;
		height: 0.5rem;
		overflow: hidden;
		border-radius: 999px;
		background: color-mix(in oklch, var(--k-ink) 12%, transparent);
	}

	.fill {
		height: 100%;
		border-radius: 999px;
		background: var(--k-accent);
	}

	.meter .value {
		width: 3.6rem;
		flex-shrink: 0;
		text-align: end;
	}

	/* Heatmap --------------------------------------------------------------- */
	.heat .sticky {
		position: sticky;
		inset-inline-start: 0;
		z-index: 1;
		border-inline-end: 1px solid var(--k-line);
		background: var(--heat-base);
		color: var(--k-ink);
		font-weight: 600;
		text-transform: capitalize;
	}

	.heat thead .sticky {
		color: var(--k-muted);
		font-weight: 500;
		text-transform: none;
	}

	.cell {
		border-inline-start: 1px solid var(--k-line);
		text-align: center;
	}

	/* Chart ----------------------------------------------------------------- */
	.chart {
		width: 100%;
		max-height: 22rem;
	}

	.tick {
		fill: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 20px;
	}

	.tick.value {
		fill: var(--k-ink);
		font-variant-numeric: tabular-nums;
	}

	.bar-gpu {
		fill: var(--k-accent);
	}

	.bar-wall {
		fill: oklch(0.72 0.11 205);
	}
</style>
