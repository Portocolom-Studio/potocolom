<script lang="ts">
	import SiteLandingHeader from '$lib/components/SiteLandingHeader.svelte';
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
	import { Badge } from '$lib/components/ui/badge';
	import * as Card from '$lib/components/ui/card';

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

<SiteLandingHeader current="benchmark" />

<div class="mx-auto max-w-6xl px-4 pt-24 sm:px-6 sm:pt-28">
	<h1 class="max-w-3xl text-4xl font-bold tracking-tight sm:text-5xl">{benchmarkTitle}</h1>
	<p class="text-muted-foreground mt-4 max-w-2xl text-lg leading-relaxed">{t('bench.sub')}</p>

	{#if hasData && report}
		<div class="mt-6 flex flex-wrap gap-2.5">
			{#if runDate}
				<Badge variant="outline">{t('bench.run')}: {runDate}</Badge>
			{/if}
			{#if report.target_vram_gb}
				<Badge variant="outline">{report.target_vram_gb} GB VRAM</Badge>
			{/if}
			<Badge variant="outline">
				{report.succeeded}/{report.total_jobs}
				{t('bench.images')}
			</Badge>
		</div>
	{/if}
</div>

<div class="mx-auto grid max-w-6xl gap-12 px-4 py-12 sm:px-6 lg:grid-cols-[220px_1fr]">
	<aside class="hidden self-start lg:sticky lg:top-20 lg:block" aria-label={t('bench.toc')}>
		<p class="text-muted-foreground text-xs font-semibold tracking-[0.18em] uppercase">
			{t('bench.toc')}
		</p>
		<ol class="mt-3 flex flex-col border-l">
			{#each tocSections as section (section.id)}
				<li>
					<a
						class="text-muted-foreground hover:text-foreground hover:border-primary -ml-px block border-l-2 border-transparent py-1.5 pl-4 font-mono text-xs transition-colors"
						href="#{section.id}"
					>
						{section.label}
					</a>
				</li>
			{/each}
		</ol>
	</aside>

	<article class="min-w-0 pb-12">
		{#if !hasData || !report}
			<Card.Root class="mb-14 [--card-spacing:--spacing(6)]">
				<Card.Header>
					<Card.Title>{t('bench.empty_title')}</Card.Title>
					<Card.Description class="text-base leading-relaxed">
						{t('bench.empty_body')}
					</Card.Description>
				</Card.Header>
			</Card.Root>
		{:else}
			<section id="bench-charts" class="mb-14 scroll-mt-20">
				<h2 class="text-2xl font-semibold">{t('bench.charts')}</h2>
				<p class="text-muted-foreground mt-3 max-w-[68ch] text-base leading-relaxed">
					{t('bench.charts_note')}
				</p>
				<div class="mt-6">
					<BenchmarkComparisons {report} />
				</div>
			</section>

			<section class="mb-14 space-y-3">
				<h2 class="text-lg font-semibold">{t('bench.details')}</h2>
				<p class="text-muted-foreground text-sm">{t('bench.details_note')}</p>
				{#each report.models as modelId (modelId)}
					{@const stats = report.model_stats.find((row) => row.model_id === modelId)}
					{@const prompts = promptAverages(modelId, report.results)}
					{@const variants = variantAverages(modelId, report.results)}
					<details id={modelId} class="group scroll-mt-20 rounded-xl border">
						<summary
							class="hover:bg-muted/30 flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 [&::-webkit-details-marker]:hidden"
						>
							<div class="flex min-w-0 items-center gap-2">
								<span class="font-mono text-sm font-medium">{modelId}</span>
								{#if isReferenceOnlyModel(modelId)}
									<Badge variant="secondary">{t('bench.reference_badge')}</Badge>
								{/if}
							</div>
							{#if stats}
								<span class="text-muted-foreground shrink-0 font-mono text-xs tabular-nums">
									{formatMs(stats.avg_gpu_ms)} gpu · {formatSeconds(stats.avg_wall_s)} wall
								</span>
							{/if}
						</summary>
						<div class="space-y-4 border-t px-4 py-4">
							<div class="overflow-x-auto rounded-lg border">
								<table class="w-full min-w-[400px] text-left text-sm">
									<thead class="bg-muted/40 border-b">
										<tr>
											<th class="px-3 py-2 font-medium">{t('bench.col_variant')}</th>
											<th class="px-3 py-2 font-medium">{t('bench.col_gpu_avg')}</th>
										</tr>
									</thead>
									<tbody>
										{#each variants as row (row.variant)}
											<tr class="border-b last:border-b-0">
												<td class="px-3 py-2 font-mono text-xs">{row.variant}</td>
												<td class="px-3 py-2 tabular-nums">{formatMs(row.avg_gpu_ms)}</td>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
							<div class="overflow-x-auto rounded-lg border">
								<table class="w-full min-w-[480px] text-left text-sm">
									<thead class="bg-muted/40 border-b">
										<tr>
											<th class="px-3 py-2 font-medium">#</th>
											<th class="px-3 py-2 font-medium">{t('bench.by_prompt')}</th>
											<th class="px-3 py-2 font-medium">{t('bench.col_gpu_avg')}</th>
										</tr>
									</thead>
									<tbody>
										{#each prompts as prompt (prompt.id)}
											<tr class="border-b last:border-b-0">
												<td class="text-muted-foreground px-3 py-2 tabular-nums">{prompt.id}</td>
												<td class="px-3 py-2">
													<p class="text-sm">{prompt.title}</p>
													<p class="text-muted-foreground text-xs">{prompt.category}</p>
												</td>
												<td class="px-3 py-2 font-mono text-xs tabular-nums">
													{formatMs(prompt.avg_gpu_ms)}
												</td>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
						</div>
					</details>
				{/each}
			</section>
		{/if}

		<section id="bench-specs" class="scroll-mt-20">
			<h2 class="text-2xl font-semibold">{t('bench.specs')}</h2>
			<p class="text-muted-foreground mt-3 max-w-[68ch] text-base leading-relaxed">
				{t('bench.specs_note')}
			</p>
			<Card.Root class="mt-6 overflow-hidden p-0 [--card-spacing:0]">
				<div class="overflow-x-auto">
					<table class="w-full min-w-[960px] text-left text-sm">
						<thead class="bg-muted/40 border-b">
							<tr>
								<th class="px-4 py-3 font-medium">{t('bench.col_model')}</th>
								<th class="px-4 py-3 font-medium">{t('bench.col_arch')}</th>
								<th class="px-4 py-3 font-medium">{t('bench.col_params')}</th>
								<th class="px-4 py-3 font-medium">{t('bench.col_vram')}</th>
								<th class="px-4 py-3 font-medium">{t('bench.col_resolution')}</th>
								<th class="px-4 py-3 font-medium">{t('bench.col_steps')}</th>
								<th class="px-4 py-3 font-medium">{t('bench.col_capabilities')}</th>
								<th class="px-4 py-3 font-medium">{t('bench.col_license')}</th>
								<th class="px-4 py-3 font-medium">{t('bench.col_commercial')}</th>
								<th class="px-4 py-3 font-medium">{t('bench.col_studio')}</th>
							</tr>
						</thead>
						<tbody>
							{#each MODEL_SPECS as spec (spec.id)}
								<tr
									class="border-b last:border-b-0"
									class:opacity-60={hasData && !benchmarkedModels.has(spec.id)}
								>
									<td class="px-4 py-3">
										<p class="font-medium">{spec.name}</p>
										<p class="text-muted-foreground mt-0.5 font-mono text-xs">{spec.id}</p>
										{#if isReferenceOnlyModel(spec.id)}
											<Badge class="mt-1" variant="secondary">{t('bench.reference_badge')}</Badge>
										{/if}
										{#if hasData && !benchmarkedModels.has(spec.id)}
											<p class="text-muted-foreground mt-1 text-xs">{t('bench.no_timing')}</p>
										{/if}
									</td>
									<td class="px-4 py-3">{spec.architecture}</td>
									<td class="px-4 py-3 tabular-nums">{spec.parameters}</td>
									<td class="px-4 py-3 tabular-nums">{spec.min_vram_gb} GB</td>
									<td class="px-4 py-3 tabular-nums">{spec.resolutions}</td>
									<td class="px-4 py-3 tabular-nums">{spec.step_range}</td>
									<td class="px-4 py-3 text-xs">{formatCapabilities(spec.capabilities)}</td>
									<td class="px-4 py-3 text-xs">{spec.license}</td>
									<td class="px-4 py-3 text-xs">{spec.commercial}</td>
									<td class="px-4 py-3 text-xs">
										{spec.studio ? t('bench.studio_yes') : t('bench.studio_no')}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</Card.Root>
		</section>
	</article>
</div>

<ScrollToTop />
