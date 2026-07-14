<script lang="ts">
	import { resolve } from '$app/paths';
	import BarChart3Icon from '@lucide/svelte/icons/bar-chart-3';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import { t } from '$lib/i18n.svelte';
	import { formatMs, formatSeconds, leaderboardRows, type BenchmarkReport } from '$lib/benchmark';
	import { computeSessionMetrics } from '$lib/studio-session-metrics';
	import { studio } from '$lib/studio.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Sheet from '$lib/components/ui/sheet';
	import * as ToggleGroup from '$lib/components/ui/toggle-group';

	let open = $state(false);
	let panel = $state<'runtime' | 'benchmarks'>('runtime');
	let benchmarkReport = $state<BenchmarkReport | null>(null);
	let benchmarkError = $state(false);

	const session = $derived(computeSessionMetrics(studio.history));
	const benchmarkRows = $derived(
		benchmarkReport ? leaderboardRows(benchmarkReport.model_stats) : []
	);
	const maxModelGpu = $derived(Math.max(...session.byModel.map((row) => row.avgGpuMs), 1));

	async function loadBenchmarkReport(): Promise<void> {
		if (benchmarkReport !== null || benchmarkError) return;
		try {
			const response = await fetch('/benchmark/results.json');
			if (!response.ok) {
				benchmarkError = true;
				return;
			}
			benchmarkReport = (await response.json()) as BenchmarkReport;
		} catch {
			benchmarkError = true;
		}
	}

	$effect(() => {
		if (open && panel === 'benchmarks') {
			void loadBenchmarkReport();
		}
	});
</script>

<Sheet.Root bind:open>
	<Sheet.Trigger>
		{#snippet child({ props })}
			<Button {...props} variant="ghost" size="icon" class="size-8" title={t('app.metrics.open')}>
				<BarChart3Icon />
				<span class="sr-only">{t('app.metrics.open')}</span>
			</Button>
		{/snippet}
	</Sheet.Trigger>
	<Sheet.Content side="right" class="w-full gap-0 overflow-y-auto p-0 sm:max-w-3xl">
		<Sheet.Header class="border-border border-b px-6 py-4">
			<Sheet.Title>{t('app.metrics.title')}</Sheet.Title>
			<Sheet.Description>{t('app.metrics.sub')}</Sheet.Description>
			<ToggleGroup.Root
				type="single"
				variant="outline"
				spacing={0}
				class="mt-4 flex w-full"
				value={panel}
				onValueChange={(value) => value && (panel = value as 'runtime' | 'benchmarks')}
			>
				<ToggleGroup.Item value="runtime" class="min-w-0 flex-1">
					{t('app.metrics.tab_runtime')}
				</ToggleGroup.Item>
				<ToggleGroup.Item value="benchmarks" class="min-w-0 flex-1">
					{t('app.metrics.tab_benchmarks')}
				</ToggleGroup.Item>
			</ToggleGroup.Root>
		</Sheet.Header>

		<div class="flex flex-col gap-6 px-6 py-6">
			{#if panel === 'runtime'}
				<div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
					<Card.Root class="gap-1 py-4">
						<Card.Header class="px-4 pb-0">
							<Card.Description>{t('app.metrics.images')}</Card.Description>
							<Card.Title class="text-2xl tabular-nums">{session.count}</Card.Title>
						</Card.Header>
					</Card.Root>
					<Card.Root class="gap-1 py-4">
						<Card.Header class="px-4 pb-0">
							<Card.Description>{t('app.metrics.total_gpu')}</Card.Description>
							<Card.Title class="text-2xl tabular-nums">
								{formatMs(session.totalGpuMs || null)}
							</Card.Title>
						</Card.Header>
					</Card.Root>
					<Card.Root class="gap-1 py-4">
						<Card.Header class="px-4 pb-0">
							<Card.Description>{t('app.metrics.avg_gpu')}</Card.Description>
							<Card.Title class="text-2xl tabular-nums">{formatMs(session.avgGpuMs)}</Card.Title>
						</Card.Header>
					</Card.Root>
					<Card.Root class="gap-1 py-4">
						<Card.Header class="px-4 pb-0">
							<Card.Description>{t('app.metrics.median_gpu')}</Card.Description>
							<Card.Title class="text-2xl tabular-nums">{formatMs(session.medianGpuMs)}</Card.Title>
						</Card.Header>
					</Card.Root>
				</div>

				{#if session.byModel.length > 0}
					<section class="flex flex-col gap-3">
						<h3 class="text-sm font-medium">{t('app.metrics.by_model')}</h3>
						<div class="flex flex-col gap-2">
							{#each session.byModel as row (row.modelId)}
								<div class="border-border rounded-lg border px-3 py-2">
									<div class="flex items-center justify-between gap-3 text-sm">
										<span class="font-medium">{row.modelId}</span>
										<span class="text-muted-foreground tabular-nums">
											{formatMs(row.avgGpuMs)} avg / {row.count}
											{t('app.metrics.samples')}
										</span>
									</div>
									<div class="bg-muted mt-2 h-2 rounded-full">
										<div
											class="bg-primary h-2 rounded-full"
											style={`width: ${Math.max(8, (row.avgGpuMs / maxModelGpu) * 100)}%`}
										></div>
									</div>
								</div>
							{/each}
						</div>
					</section>
				{/if}

				<section class="flex flex-col gap-3">
					<h3 class="text-sm font-medium">{t('app.metrics.recent')}</h3>
					{#if session.recent.length === 0}
						<p class="text-muted-foreground text-sm">{t('app.metrics.runtime_empty')}</p>
					{:else}
						<div class="border-border overflow-x-auto rounded-lg border">
							<table class="w-full min-w-[32rem] text-sm">
								<thead class="bg-muted/40 text-muted-foreground text-left text-xs uppercase">
									<tr>
										<th class="px-3 py-2 font-medium">{t('app.metrics.col_model')}</th>
										<th class="px-3 py-2 font-medium">{t('app.metrics.col_size')}</th>
										<th class="px-3 py-2 font-medium">{t('app.metrics.col_steps')}</th>
										<th class="px-3 py-2 font-medium">{t('app.metrics.col_gpu')}</th>
										<th class="px-3 py-2 font-medium">{t('app.metrics.col_prompt')}</th>
									</tr>
								</thead>
								<tbody>
									{#each session.recent as row (row.id)}
										<tr class="border-border border-t">
											<td class="px-3 py-2 font-medium">{row.modelId}</td>
											<td class="px-3 py-2 tabular-nums">{row.width} x {row.height}</td>
											<td class="px-3 py-2 tabular-nums">{row.steps ?? '-'}</td>
											<td class="px-3 py-2 tabular-nums">{formatMs(row.gpuMs)}</td>
											<td class="text-muted-foreground max-w-[12rem] truncate px-3 py-2">
												{row.prompt || '-'}
											</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					{/if}
				</section>
			{:else if benchmarkReport}
				<div class="flex flex-wrap items-center gap-2">
					{#if benchmarkReport.target_vram_gb}
						<Badge variant="secondary">{benchmarkReport.target_vram_gb} GB VRAM</Badge>
					{/if}
					<Badge variant="secondary">
						{benchmarkReport.succeeded}/{benchmarkReport.total_jobs}
						{t('app.metrics.benchmark_images')}
					</Badge>
					<Button href={resolve('/benchmark')} variant="outline" size="sm" class="ms-auto">
						<ExternalLinkIcon />
						{t('app.metrics.open_benchmark_page')}
					</Button>
				</div>

				<div class="border-border overflow-x-auto rounded-lg border">
					<table class="w-full min-w-[36rem] text-sm">
						<thead class="bg-muted/40 text-muted-foreground text-left text-xs uppercase">
							<tr>
								<th class="px-3 py-2 font-medium">#</th>
								<th class="px-3 py-2 font-medium">{t('app.metrics.col_model')}</th>
								<th class="px-3 py-2 font-medium">{t('app.metrics.col_gpu_avg')}</th>
								<th class="px-3 py-2 font-medium">{t('app.metrics.col_wall')}</th>
								<th class="px-3 py-2 font-medium">{t('app.metrics.col_load')}</th>
							</tr>
						</thead>
						<tbody>
							{#each benchmarkRows as row (row.model_id)}
								<tr class="border-border border-t">
									<td class="px-3 py-2 tabular-nums">{row.rank}</td>
									<td class="px-3 py-2 font-medium">
										{row.model_id}
										{#if row.reference}
											<Badge class="ms-2" variant="outline">{t('bench.reference_badge')}</Badge>
										{/if}
									</td>
									<td class="px-3 py-2">
										<div class="flex items-center gap-2">
											<div class="bg-muted h-2 w-24 rounded-full">
												<div
													class="bg-primary h-2 rounded-full"
													style={`width: ${Math.max(8, row.gpu_ratio * 100)}%`}
												></div>
											</div>
											<span class="tabular-nums">{row.gpu_display}</span>
										</div>
									</td>
									<td class="px-3 py-2 tabular-nums">{row.wall_display}</td>
									<td class="px-3 py-2 tabular-nums">{row.load_display}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{:else if benchmarkError}
				<p class="text-muted-foreground text-sm">{t('app.metrics.benchmark_empty')}</p>
			{:else}
				<p class="text-muted-foreground text-sm">{t('app.metrics.benchmark_loading')}</p>
			{/if}
		</div>
	</Sheet.Content>
</Sheet.Root>
