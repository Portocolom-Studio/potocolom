<script lang="ts">
	import {
		categoryLineSeries,
		formatMs,
		leaderboardRows,
		type BenchmarkReport
	} from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';

	let { report }: { report: BenchmarkReport } = $props();

	const rows = $derived(leaderboardRows(report.model_stats));
	const lineData = $derived(categoryLineSeries(report.results, report.models));

	function statsFor(modelId: string) {
		return report.model_stats.find((row) => row.model_id === modelId);
	}

	function categoryMs(modelId: string, category: string): number {
		return (
			lineData.series
				.find((series) => series.model_id === modelId)
				?.points.find((point) => point.category === category)?.avg_gpu_ms ?? 0
		);
	}

	/** Ring and tick values at 1, 2, 5 per decade inside the range. */
	function niceTicks(lo: number, hi: number): number[] {
		const out: number[] = [];
		for (
			let exponent = Math.floor(Math.log10(lo));
			exponent <= Math.ceil(Math.log10(hi));
			exponent += 1
		) {
			for (const mantissa of [1, 2, 5]) {
				const value = mantissa * 10 ** exponent;
				if (value >= lo && value <= hi) out.push(value);
			}
		}
		return out;
	}

	/* Times here span 300 ms to 14 s, so every scale is log. On a linear one the
	   fast half of the field collapses into the origin and says nothing. */
	function logPosition(ms: number, lo: number, hi: number): number {
		if (ms <= 0) return 0;
		const span = Math.log(hi) - Math.log(lo);
		if (span <= 0) return 0;
		return Math.min(1, Math.max(0, (Math.log(ms) - Math.log(lo)) / span));
	}

	/* Radar ------------------------------------------------------------------
	   One axis per model, two rings: GPU denoise and end-to-end wall. Both are
	   milliseconds on one shared log scale, so the gap between the polygons is
	   the overhead around denoising and a smaller polygon is simply faster. */
	const radarW = 700;
	const radarH = 560;
	const rcx = radarW / 2;
	const rcy = radarH / 2;
	const radarR = 214;
	const labelR = radarR + 30;

	/* The floor drops to the decade below the fastest model. Hugging the minimum
	   instead would park the two turbo models on the origin, where a radar has no
	   room to say anything. */
	const radarLo = $derived(
		10 ** Math.floor(Math.log10(Math.max(1, Math.min(...rows.map((r) => r.gpu_ms), Infinity))))
	);
	const radarHi = $derived(Math.max(...rows.map((r) => r.wall_s * 1000), 1) * 1.3);
	const radarRings = $derived(niceTicks(radarLo, radarHi).filter((ring) => ring > radarLo));

	function axisAngle(index: number, count: number): number {
		return -Math.PI / 2 + ((2 * Math.PI) / count) * index;
	}

	function radarPoint(index: number, count: number, scale: number) {
		const angle = axisAngle(index, count);
		return { x: rcx + Math.cos(angle) * radarR * scale, y: rcy + Math.sin(angle) * radarR * scale };
	}

	function ringPath(scale: number, count: number): string {
		return (
			Array.from({ length: count }, (_, index) => {
				const point = radarPoint(index, count, scale);
				return `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`;
			}).join(' ') + ' Z'
		);
	}

	function seriesPath(values: number[]): string {
		return (
			values
				.map((ms, index) => {
					const point = radarPoint(index, values.length, logPosition(ms, radarLo, radarHi));
					return `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`;
				})
				.join(' ') + ' Z'
		);
	}

	const gpuSeries = $derived(rows.map((row) => row.gpu_ms));
	const wallSeries = $derived(rows.map((row) => row.wall_s * 1000));

	/** Labels sit outside the ring, so anchor them by which side they land on. */
	function labelAnchor(index: number, count: number): 'start' | 'middle' | 'end' {
		const x = Math.cos(axisAngle(index, count));
		if (x > 0.15) return 'start';
		if (x < -0.15) return 'end';
		return 'middle';
	}

	/* Spread -----------------------------------------------------------------
	   One row per model, one dot per prompt category. How far the dots scatter
	   along the row is the thing the averages hide. */
	const dotLabelW = 150;
	const dotRowH = 48;
	const dotPlotW = 560;
	const dotAxisH = 34;
	const dotW = dotLabelW + dotPlotW + 24;
	const dotH = $derived(rows.length * dotRowH + dotAxisH);

	const dotLo = $derived(
		Math.max(
			1,
			Math.min(
				...lineData.series.flatMap((s) => s.points.map((p) => p.avg_gpu_ms)).filter((ms) => ms > 0),
				Infinity
			) * 0.8
		)
	);
	const dotHi = $derived(
		Math.max(...lineData.series.flatMap((s) => s.points.map((p) => p.avg_gpu_ms)), 1) * 1.25
	);
	const dotTicks = $derived(niceTicks(dotLo, dotHi));

	function dotX(ms: number): number {
		return dotLabelW + logPosition(ms, dotLo, dotHi) * dotPlotW;
	}

	/* Most models land every category within a few milliseconds of each other, so
	   the dots pile up. The range bar keeps that readable: a tight cluster draws
	   a short bar rather than an ambiguous smudge, and an outlier stretches it. */
	function rowRange(modelId: string): { min: number; max: number } {
		const values = lineData.categories
			.map((category) => categoryMs(modelId, category))
			.filter((ms) => ms > 0);
		return { min: Math.min(...values), max: Math.max(...values) };
	}

	/* Categories usually land within a few ms of each other, which would stack all
	   nine dots into one. Fanning them down the row keeps every category visible
	   and keeps a category at the same height on every row. */
	function dotY(rowIndex: number, categoryIndex: number, count: number): number {
		const centred = categoryIndex - (count - 1) / 2;
		return rowIndex * dotRowH + dotRowH / 2 + centred * 3;
	}

	/** Distinct hue per category; stop short of a full turn so the ends differ. */
	function categoryColor(index: number, count: number): string {
		return `oklch(var(--dot-l) 0.15 ${Math.round((310 / Math.max(count - 1, 1)) * index + 20)})`;
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
				<span class="quiet">{t('bench.radar_scale')}</span>
			</div>
		</header>
		<div class="pad">
			<svg
				viewBox="0 0 {radarW} {radarH}"
				class="radar"
				role="img"
				aria-label={t('bench.chart_grouped')}
			>
				{#each radarRings as ring (ring)}
					<path d={ringPath(logPosition(ring, radarLo, radarHi), rows.length)} class="grid" />
				{/each}

				{#each rows as row, i (row.model_id)}
					{@const outer = radarPoint(i, rows.length, 1)}
					{@const label = radarPoint(i, rows.length, labelR / radarR)}
					<line x1={rcx} y1={rcy} x2={outer.x} y2={outer.y} class="grid" />
					<text
						x={label.x}
						y={label.y}
						class="tick"
						text-anchor={labelAnchor(i, rows.length)}
						dominant-baseline="middle"
					>
						{row.model_id}
					</text>
				{/each}

				<path d={seriesPath(wallSeries)} class="area-wall" />
				<path d={seriesPath(gpuSeries)} class="area-gpu" />

				{#each rows as row, i (row.model_id)}
					{@const wall = radarPoint(
						i,
						rows.length,
						logPosition(row.wall_s * 1000, radarLo, radarHi)
					)}
					{@const gpu = radarPoint(i, rows.length, logPosition(row.gpu_ms, radarLo, radarHi))}
					<circle cx={wall.x} cy={wall.y} r="4" class="dot-wall">
						<title>{row.model_id} wall: {row.wall_display}</title>
					</circle>
					<circle cx={gpu.x} cy={gpu.y} r="4" class="dot-gpu">
						<title>{row.model_id} GPU: {row.gpu_display}</title>
					</circle>
				{/each}

				<!-- Last, so the halo lands on top of whatever the first axis draws. -->
				{#each radarRings as ring (ring)}
					{@const scale = logPosition(ring, radarLo, radarHi)}
					<text x={rcx + 7} y={rcy - radarR * scale} class="tick ring-label">{formatMs(ring)}</text>
				{/each}
			</svg>
		</div>
	</section>

	<section class="card">
		<header>
			<h3>{t('bench.chart_spread')}</h3>
			<p>{t('bench.chart_spread_desc')}</p>
			<div class="legend">
				{#each lineData.categories as category, i (category)}
					<span>
						<i class="swatch dot" style:background={categoryColor(i, lineData.categories.length)}
						></i>
						{category}
					</span>
				{/each}
			</div>
		</header>
		<div class="scroll">
			<div class="pad">
				<svg
					viewBox="0 0 {dotW} {dotH}"
					class="dots"
					style:min-width="34rem"
					role="img"
					aria-label={t('bench.chart_spread')}
				>
					{#each dotTicks as tick (tick)}
						{@const x = dotX(tick)}
						<line x1={x} y1="0" x2={x} y2={dotH - dotAxisH} class="grid" />
						<text {x} y={dotH - 10} class="tick" text-anchor="middle">{formatMs(tick)}</text>
					{/each}

					{#each rows as row, i (row.model_id)}
						{@const y = i * dotRowH + dotRowH / 2}
						{@const range = rowRange(row.model_id)}
						<text x="0" {y} class="tick" dominant-baseline="middle">{row.model_id}</text>
						<line x1={dotX(range.min)} y1={y} x2={dotX(range.max)} y2={y} class="range-bar">
							<title>
								{row.model_id}: {formatMs(range.min)} to {formatMs(range.max)} across categories
							</title>
						</line>
						{#each lineData.categories as category, c (category)}
							{@const ms = categoryMs(row.model_id, category)}
							{#if ms > 0}
								<circle
									cx={dotX(ms)}
									cy={dotY(i, c, lineData.categories.length)}
									r="4.5"
									class="dot"
									style:fill={categoryColor(c, lineData.categories.length)}
								>
									<title>{row.model_id} {category}: {formatMs(ms)}</title>
								</circle>
							{/if}
						{/each}
					{/each}
				</svg>
			</div>
		</div>
		<details>
			<summary>{t('bench.spread_numbers')}</summary>
			<div class="scroll">
				<table class="grid-table">
					<thead>
						<tr>
							<th scope="col" class="sticky">{t('bench.heatmap_category')}</th>
							{#each rows as row (row.model_id)}
								<th scope="col" class="mono">{row.model_id}</th>
							{/each}
						</tr>
					</thead>
					<tbody>
						{#each lineData.categories as category (category)}
							<tr>
								<th scope="row" class="sticky">{category}</th>
								{#each rows as row (row.model_id)}
									<td class="mono num">{formatMs(categoryMs(row.model_id, category))}</td>
								{/each}
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</details>
	</section>
</div>

<style>
	/* Hallmark - the benchmark charts on the latent tokens, so they belong to the page. */
	.stack {
		--dot-l: 0.76;
		--wall-tone: oklch(0.72 0.11 205);
		display: grid;
		gap: 1rem;
		min-width: 0;
	}

	:global(:root[data-krea-mode='light']) .stack {
		--dot-l: 0.58;
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
		gap: 0.5rem 0.85rem;
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

	.swatch.dot {
		width: 0.55rem;
		height: 0.55rem;
		border-radius: 999px;
	}

	.swatch.gpu {
		background: var(--k-accent);
	}

	.swatch.wall {
		background: var(--wall-tone);
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

	.grid-table {
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

	details {
		border-block-start: 1px solid var(--k-line);
	}

	summary {
		padding: 0.7rem 1.1rem;
		color: var(--k-muted);
		font-size: 0.8rem;
		cursor: pointer;
	}

	.grid-table .sticky {
		position: sticky;
		inset-inline-start: 0;
		background: var(--k-paper);
		color: var(--k-ink);
		text-transform: capitalize;
	}

	.grid-table thead .sticky {
		color: var(--k-muted);
		text-transform: none;
	}

	/* Charts ---------------------------------------------------------------- */
	.radar {
		width: 100%;
		max-height: 30rem;
	}

	.dots {
		width: 100%;
	}

	.grid {
		fill: none;
		stroke: var(--k-line);
		stroke-width: 1;
	}

	.tick {
		fill: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 13px;
	}

	/* The labels run up the first model's axis, so give them a halo to sit in. */
	.ring-label {
		paint-order: stroke;
		stroke: var(--k-panel);
		stroke-width: 4px;
		stroke-linejoin: round;
		font-size: 11px;
	}

	.area-gpu {
		fill: color-mix(in oklch, var(--k-accent) 22%, transparent);
		stroke: var(--k-accent);
		stroke-width: 2;
	}

	.area-wall {
		fill: color-mix(in oklch, var(--wall-tone) 16%, transparent);
		stroke: var(--wall-tone);
		stroke-width: 2;
		stroke-dasharray: 5 4;
	}

	.dot-gpu {
		fill: var(--k-accent);
	}

	.dot-wall {
		fill: var(--wall-tone);
	}

	.dot {
		fill-opacity: 0.8;
		stroke: var(--k-panel);
		stroke-width: 1;
	}

	.range-bar {
		stroke: var(--k-line);
		stroke-width: 10;
		stroke-linecap: round;
	}
</style>
