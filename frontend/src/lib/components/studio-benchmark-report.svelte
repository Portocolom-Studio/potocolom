<script lang="ts">
	import { resolve } from '$app/paths';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import { leaderboardRows, type BenchmarkReport } from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';
	import StudioBenchmarkChart from '$lib/components/studio-benchmark-chart.svelte';
	import StudioBenchmarkRunTimeline from '$lib/components/studio-benchmark-run-timeline.svelte';
	import { Button } from '$lib/components/ui/button';

	let {
		sessionId,
		report,
		clientHeight = $bindable(0)
	}: {
		sessionId: string;
		report: BenchmarkReport;
		clientHeight?: number;
	} = $props();

	const benchmarkRows = $derived(leaderboardRows(report.model_stats));
</script>

<div
	data-benchmark-report={sessionId}
	class="benchmark-report flex flex-col gap-4"
	bind:clientHeight
>
	<div class="flex flex-wrap items-center gap-2">
		{#if report.target_vram_gb}
			<span class="tag">{report.target_vram_gb} GB VRAM</span>
		{/if}
		<span class="tag tabular-nums">
			{report.succeeded}/{report.total_jobs}
			{t('app.metrics.benchmark_images')}
		</span>
		<Button href={resolve('/benchmark')} variant="outline" size="sm" class="ms-auto">
			<ExternalLinkIcon />
			{t('app.metrics.open_benchmark_page')}
		</Button>
	</div>

	<StudioBenchmarkChart stats={report.model_stats} />

	<StudioBenchmarkRunTimeline {report} />

	<div
		class="bg-card border-border overflow-x-auto rounded-[10px] border"
		tabindex="0"
		role="region"
		aria-label={t('studio.bench_table_region')}
	>
		<table data-benchmark-table class="w-full min-w-[32rem] text-sm">
			<thead class="text-muted-foreground border-border border-b text-left text-xs">
				<tr>
					<th class="px-3 py-2 font-medium">#</th>
					<th class="px-3 py-2 font-medium">{t('app.metrics.col_model')}</th>
					<th class="px-3 py-2 font-medium">{t('app.metrics.col_gpu_avg')}</th>
					<th class="px-3 py-2 font-medium">{t('app.metrics.col_wall')}</th>
				</tr>
			</thead>
			<tbody>
				{#each benchmarkRows as row (row.model_id)}
					<tr class="border-border border-t">
						<td class="px-3 py-2 tabular-nums">{row.rank}</td>
						<td class="px-3 py-2 font-medium">{row.model_id}</td>
						<td class="px-3 py-2 tabular-nums">{row.gpu_display}</td>
						<td class="px-3 py-2 tabular-nums">{row.wall_display}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
</div>

<style>
	@keyframes benchmark-report-enter {
		from {
			opacity: 0.4;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.benchmark-report {
		animation: benchmark-report-enter 200ms ease-out both;
	}

	@media (prefers-reduced-motion: reduce) {
		.benchmark-report {
			animation: none;
		}
	}
</style>
