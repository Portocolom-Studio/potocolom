<script lang="ts">
	import LatentShell from '$lib/components/landing-krea/LatentShell.svelte';
	import ScrollToTop from '$lib/components/ScrollToTop.svelte';
	import BenchmarkComparisons from '$lib/components/benchmark-comparisons.svelte';
	import {
		formatMs,
		formatSeconds,
		promptAverages,
		variantAverages,
		isReferenceOnlyModel,
		type BenchmarkReport
	} from '$lib/benchmark';
	import { formatCapabilities, MODEL_SPECS } from '$lib/model-specs';
	import { t } from '$lib/i18n.svelte';
	import '../../krea-tokens.css';

	let { data } = $props();

	const report = $derived(data.report as BenchmarkReport | null);
	const hasData = $derived(Boolean(report && report.results.length > 0));

	const runDate = $derived(
		report?.created_at
			? new Date(report.created_at).toLocaleString(undefined, {
					dateStyle: 'medium',
					timeStyle: 'short'
				})
			: null
	);
	const benchmarkTitle = $derived(
		t('bench.title').replace('{vram}', String(report?.target_vram_gb ?? 16))
	);
	const benchmarkedModels = $derived(new Set(report?.models ?? []));

	const tocSections = $derived.by(() => {
		const items: { id: string; label: string }[] = [];
		if (hasData) {
			items.push({ id: 'bench-charts', label: t('bench.toc_charts') });
			if (report) {
				for (const modelId of report.models) {
					items.push({ id: modelId, label: modelId });
				}
			}
		}
		items.push({ id: 'bench-specs', label: t('bench.toc_specs') });
		return items;
	});
</script>

<svelte:head>
	<title>potocolom - {benchmarkTitle}</title>
	<meta name="description" content={t('bench.sub')} />
</svelte:head>

<LatentShell current="benchmark">
	<main>
		<section class="opening">
			<h1>{benchmarkTitle}</h1>
			<p class="lede">{t('bench.sub')}</p>
			{#if hasData && report}
				<div class="chips">
					{#if runDate}
						<span class="chip">{t('bench.run')}: {runDate}</span>
					{/if}
					{#if report.target_vram_gb}
						<span class="chip">{report.target_vram_gb} GB VRAM</span>
					{/if}
					<span class="chip">
						{report.succeeded}/{report.total_jobs}
						{t('bench.images')}
					</span>
				</div>
			{/if}
		</section>

		<div class="panel document">
			<aside aria-label={t('bench.toc')}>
				<p class="rail-label">{t('bench.toc')}</p>
				<ol>
					{#each tocSections as section (section.id)}
						<li><a href="#{section.id}">{section.label}</a></li>
					{/each}
				</ol>
			</aside>

			<article>
				{#if !hasData || !report}
					<section class="empty">
						<h2>{t('bench.empty_title')}</h2>
						<p>{t('bench.empty_body')}</p>
					</section>
				{:else}
					<section id="bench-charts">
						<h2>{t('bench.charts')}</h2>
						<p>{t('bench.charts_note')}</p>
						<div class="charts"><BenchmarkComparisons {report} /></div>
					</section>

					<section>
						<h2>{t('bench.details')}</h2>
						<p>{t('bench.details_note')}</p>
						<div class="models">
							{#each report.models as modelId (modelId)}
								{@const stats = report.model_stats.find((row) => row.model_id === modelId)}
								{@const prompts = promptAverages(modelId, report.results)}
								{@const variants = variantAverages(modelId, report.results)}
								<details id={modelId}>
									<summary>
										<span class="model-id">
											{modelId}
											{#if isReferenceOnlyModel(modelId)}
												<span class="chip chip-quiet">{t('bench.reference_badge')}</span>
											{/if}
										</span>
										{#if stats}
											<span class="model-stat">
												{formatMs(stats.avg_gpu_ms)} gpu / {formatSeconds(stats.avg_wall_s)} wall
											</span>
										{/if}
									</summary>
									<div class="model-body">
										<div class="table-wrap">
											<table>
												<thead>
													<tr>
														<th scope="col">{t('bench.col_variant')}</th>
														<th scope="col">{t('bench.col_gpu_avg')}</th>
													</tr>
												</thead>
												<tbody>
													{#each variants as row (row.variant)}
														<tr>
															<td class="mono">{row.variant}</td>
															<td class="num">{formatMs(row.avg_gpu_ms)}</td>
														</tr>
													{/each}
												</tbody>
											</table>
										</div>
										<div class="table-wrap">
											<table>
												<thead>
													<tr>
														<th scope="col">#</th>
														<th scope="col">{t('bench.by_prompt')}</th>
														<th scope="col">{t('bench.col_gpu_avg')}</th>
													</tr>
												</thead>
												<tbody>
													{#each prompts as prompt (prompt.id)}
														<tr>
															<td class="num quiet">{prompt.id}</td>
															<td>
																<p>{prompt.title}</p>
																<p class="quiet">{prompt.category}</p>
															</td>
															<td class="num mono">{formatMs(prompt.avg_gpu_ms)}</td>
														</tr>
													{/each}
												</tbody>
											</table>
										</div>
									</div>
								</details>
							{/each}
						</div>
					</section>
				{/if}

				<section id="bench-specs">
					<h2>{t('bench.specs')}</h2>
					<p>{t('bench.specs_note')}</p>
					<div class="table-wrap wide">
						<table>
							<thead>
								<tr>
									<th scope="col">{t('bench.col_model')}</th>
									<th scope="col">{t('bench.col_arch')}</th>
									<th scope="col">{t('bench.col_params')}</th>
									<th scope="col">{t('bench.col_vram')}</th>
									<th scope="col">{t('bench.col_resolution')}</th>
									<th scope="col">{t('bench.col_steps')}</th>
									<th scope="col">{t('bench.col_capabilities')}</th>
									<th scope="col">{t('bench.col_license')}</th>
									<th scope="col">{t('bench.col_commercial')}</th>
									<th scope="col">{t('bench.col_studio')}</th>
								</tr>
							</thead>
							<tbody>
								{#each MODEL_SPECS as spec (spec.id)}
									<tr class:dimmed={hasData && !benchmarkedModels.has(spec.id)}>
										<td>
											<p class="spec-name">{spec.name}</p>
											<p class="quiet mono">{spec.id}</p>
											{#if isReferenceOnlyModel(spec.id)}
												<span class="chip chip-quiet">{t('bench.reference_badge')}</span>
											{/if}
											{#if hasData && !benchmarkedModels.has(spec.id)}
												<p class="quiet">{t('bench.no_timing')}</p>
											{/if}
										</td>
										<td>{spec.architecture}</td>
										<td class="num">{spec.parameters}</td>
										<td class="num">{spec.min_vram_gb} GB</td>
										<td class="num">{spec.resolutions}</td>
										<td class="num">{spec.step_range}</td>
										<td class="small">{formatCapabilities(spec.capabilities)}</td>
										<td class="small">{spec.license}</td>
										<td class="small">{spec.commercial}</td>
										<td class="small">
											{spec.studio ? t('bench.studio_yes') : t('bench.studio_no')}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				</section>
			</article>
		</div>
	</main>
</LatentShell>

<ScrollToTop />

<style>
	/* Hallmark - macrostructure: Latent Document - genre: abstract atmospheric - the landing's canvas and panels carry the benchmark report - contrast: pass - mobile: pass */
	main {
		position: relative;
		z-index: 1;
		display: grid;
	}

	.opening {
		display: grid;
		justify-items: start;
		gap: 1.1rem;
		max-width: 56rem;
		padding: clamp(3rem, 9vw, 6rem) clamp(1rem, 5vw, 4rem) clamp(2rem, 6vw, 4rem);
	}

	h1 {
		font-size: clamp(2.2rem, 5vw, 4rem);
		line-height: 1;
	}

	.lede {
		max-width: 56ch;
		color: var(--k-muted);
		font-size: 1.05rem;
	}

	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}

	.chip {
		display: inline-flex;
		align-items: center;
		padding: 0.3rem 0.7rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		color: var(--k-muted);
		font-size: 0.78rem;
		white-space: nowrap;
	}

	.chip-quiet {
		padding: 0.15rem 0.5rem;
		font-size: 0.68rem;
	}

	.panel {
		border-block-start: 1px solid var(--k-line);
		background: oklch(0.08 0.012 265 / 72%);
		backdrop-filter: blur(28px);
	}

	:global(:root[data-krea-mode='light']) .panel {
		background: oklch(0.97 0.004 255 / 78%);
	}

	.document {
		display: grid;
		gap: clamp(2rem, 5vw, 4rem);
		padding-block: clamp(3rem, 8vw, 5rem);
		padding-inline: max(clamp(1rem, 4vw, 3rem), calc((100% - 78rem) / 2));
	}

	aside {
		display: none;
		position: sticky;
		inset-block-start: 1.5rem;
		align-self: start;
	}

	.rail-label {
		color: var(--k-muted);
		font-size: 0.85rem;
	}

	aside ol {
		display: grid;
		margin: 0.85rem 0 0;
		padding: 0;
		list-style: none;
		border-inline-start: 1px solid var(--k-line);
	}

	aside a {
		display: block;
		margin-inline-start: -1px;
		padding: 0.4rem 0 0.4rem 0.9rem;
		border-inline-start: 2px solid transparent;
		overflow: hidden;
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.76rem;
		text-overflow: ellipsis;
		transition: color 140ms var(--k-ease);
	}

	aside a:hover {
		border-inline-start-color: var(--k-accent);
		color: var(--k-ink);
	}

	article {
		display: grid;
		gap: clamp(2.5rem, 5vw, 3.5rem);
		min-width: 0;
	}

	article > section {
		display: grid;
		gap: 0.9rem;
		min-width: 0;
		scroll-margin-block-start: 2rem;
	}

	h2 {
		font-size: clamp(1.5rem, 2.6vw, 2rem);
		line-height: 1.1;
	}

	article p {
		max-width: 68ch;
		color: var(--k-muted);
		line-height: 1.7;
	}

	.charts {
		min-width: 0;
		margin-block-start: 0.5rem;
	}

	/* Models ---------------------------------------------------------------- */
	.models {
		display: grid;
		gap: 0.6rem;
		min-width: 0;
		margin-block-start: 0.5rem;
	}

	details {
		border: 1px solid var(--k-line);
		border-radius: 0.9rem;
		background: var(--k-panel);
		scroll-margin-block-start: 2rem;
	}

	summary {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 0.85rem 1rem;
		cursor: pointer;
		list-style: none;
	}

	summary::-webkit-details-marker {
		display: none;
	}

	.model-id {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		min-width: 0;
		font-family: var(--k-mono);
		font-size: 0.85rem;
	}

	.model-stat {
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.75rem;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}

	.model-body {
		display: grid;
		gap: 1rem;
		padding: 1rem;
		border-block-start: 1px solid var(--k-line);
	}

	/* Tables ---------------------------------------------------------------- */
	.table-wrap {
		min-width: 0;
		overflow-x: auto;
		border: 1px solid var(--k-line);
		border-radius: 0.75rem;
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.88rem;
	}

	.table-wrap.wide table {
		min-width: 60rem;
	}

	th,
	td {
		padding: 0.65rem 0.85rem;
		border-block-end: 1px solid var(--k-line);
		text-align: start;
		vertical-align: top;
	}

	thead th {
		color: var(--k-muted);
		font-size: 0.76rem;
		font-weight: 500;
		white-space: nowrap;
	}

	tbody tr:last-child td {
		border-block-end: 0;
	}

	td {
		color: var(--k-ink);
	}

	td p {
		margin: 0;
		color: inherit;
	}

	.quiet {
		color: var(--k-muted);
		font-size: 0.76rem;
	}

	.mono {
		font-family: var(--k-mono);
		font-size: 0.78rem;
	}

	.num {
		font-variant-numeric: tabular-nums;
	}

	.small {
		font-size: 0.78rem;
	}

	.spec-name {
		font-weight: 600;
	}

	.dimmed {
		opacity: 0.6;
	}

	.empty {
		padding: clamp(1.5rem, 4vw, 2.5rem);
		border: 1px solid var(--k-line);
		border-radius: 1rem;
		background: var(--k-panel);
	}

	@media (min-width: 64rem) {
		.document {
			grid-template-columns: 14rem minmax(0, 1fr);
		}

		aside {
			display: block;
		}
	}
</style>
