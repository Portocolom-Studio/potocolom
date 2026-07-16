<script lang="ts">
	import { onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import { t } from '$lib/i18n.svelte';
	import { formatMs, leaderboardRows } from '$lib/benchmark';
	import { loadBenchmarkSessions, type BenchmarkSession } from '$lib/studio-benchmark-sessions';
	import {
		fetchGpuHistory,
		historyRollupForRange,
		type GpuHistoryPoint
	} from '$lib/studio-gpu-history';
	import { buildGpuTimeline } from '$lib/studio-gpu-timeline';
	import { METRICS_RANGE_MS, type MetricsRange } from '$lib/studio-metrics-range';
	import {
		gpuSamples,
		lastGpuHardware,
		startGpuSampler,
		stopGpuSampler,
		subscribeGpuSamples,
		type GpuHardware
	} from '$lib/studio-gpu-sampler';
	import { computeJobStatusBreakdown } from '$lib/studio-session-job-stats';
	import { computePipelineMetrics } from '$lib/studio-session-pipeline-metrics';
	import { computeSessionMetrics } from '$lib/studio-session-metrics';
	import { studio } from '$lib/studio.svelte';
	import StudioGpuTimelineChart from '$lib/components/studio-gpu-timeline-chart.svelte';
	import StudioJobStatusChart from '$lib/components/studio-job-status-chart.svelte';
	import StudioSessionModelChart from '$lib/components/studio-session-model-chart.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';

	let liveTick = $state(0);
	let sessions = $state<BenchmarkSession[]>([]);
	let sessionsLoading = $state(false);
	let sessionsError = $state(false);
	let selectedSessionId = $state<string | null>(null);
	let metricsRange = $state<MetricsRange>('5m');
	let persistedHistory = $state<GpuHistoryPoint[]>([]);

	const session = $derived(computeSessionMetrics(studio.history));
	const pipeline = $derived(computePipelineMetrics(studio.history));
	const jobStatus = $derived(computeJobStatusBreakdown(studio.history, t));
	const hardwareVramPct = $derived.by(() => {
		void liveTick;
		const hw: GpuHardware | null = lastGpuHardware();
		if (!hw) return null;
		if (typeof hw.vram_used_pct === 'number') return hw.vram_used_pct;
		if (
			typeof hw.vram_used_bytes === 'number' &&
			typeof hw.vram_total_bytes === 'number' &&
			hw.vram_total_bytes > 0
		) {
			return Math.round((hw.vram_used_bytes * 100) / hw.vram_total_bytes);
		}
		return null;
	});
	const timeline = $derived.by(() => {
		void liveTick;
		return buildGpuTimeline(
			studio.history,
			gpuSamples(),
			hardwareVramPct,
			metricsRange,
			persistedHistory
		);
	});
	const gpuSamplerLive = $derived(studio.metricsTab === 'usage');

	$effect(() => {
		if (studio.metricsTab !== 'usage') return;
		const range = metricsRange;
		void liveTick;
		const now = Date.now();
		const from = now - METRICS_RANGE_MS[range];
		let cancelled = false;
		void fetchGpuHistory(from, now, historyRollupForRange(range)).then((points) => {
			if (!cancelled) persistedHistory = points;
		});
		return () => {
			cancelled = true;
		};
	});
	const maxModelGpu = $derived(Math.max(...session.byModel.map((row) => row.avgGpuMs), 1));
	const selectedSession = $derived(
		sessions.find((entry) => entry.id === selectedSessionId) ?? sessions[0] ?? null
	);
	const benchmarkRows = $derived(
		selectedSession ? leaderboardRows(selectedSession.report.model_stats) : []
	);
	const latestSample = $derived.by(() => {
		void liveTick;
		const samples = gpuSamples();
		return samples.length > 0 ? samples[samples.length - 1] : null;
	});

	onMount(() => {
		const unsubscribe = subscribeGpuSamples(() => {
			liveTick += 1;
		});
		return unsubscribe;
	});

	$effect(() => {
		if (studio.metricsTab !== 'usage') {
			stopGpuSampler();
			return;
		}
		startGpuSampler(() => studio.history);
		return () => stopGpuSampler();
	});

	$effect(() => {
		if (studio.metricsTab !== 'benchmarks' || sessions.length > 0 || sessionsLoading) return;
		sessionsLoading = true;
		void loadBenchmarkSessions()
			.then((loaded) => {
				sessions = loaded;
				selectedSessionId = loaded[0]?.id ?? null;
			})
			.catch(() => {
				sessionsError = true;
			})
			.finally(() => {
				sessionsLoading = false;
			});
	});
</script>

<div class="flex flex-col gap-6">
	{#if studio.metricsTab === 'usage'}
		<StudioGpuTimelineChart
			{timeline}
			vramAvailable={hardwareVramPct != null}
			bind:range={metricsRange}
			live={gpuSamplerLive}
		/>

		{#if latestSample?.hardwareAvailable}
			<div class="flex flex-wrap gap-2">
				{#if latestSample.vramUsedPct != null}
					<Badge variant="secondary">VRAM {latestSample.vramUsedPct}%</Badge>
				{/if}
				{#if latestSample.temperatureC != null}
					<Badge variant="secondary">{latestSample.temperatureC} C</Badge>
				{/if}
				{#if latestSample.powerW != null}
					<Badge variant="secondary">{latestSample.powerW.toFixed(0)} W</Badge>
				{/if}
			</div>
		{/if}

		<div class="grid gap-4 xl:grid-cols-4">
			<div class="xl:col-span-2">
				<StudioJobStatusChart breakdown={jobStatus} />
			</div>
			<Card.Root class="gap-1 py-3">
				<Card.Header class="px-4 pb-0">
					<Card.Description>{t('app.metrics.total_gpu')}</Card.Description>
					<Card.Title class="text-2xl tabular-nums">
						{formatMs(session.totalGpuMs || null)}
					</Card.Title>
				</Card.Header>
			</Card.Root>
			<Card.Root class="gap-1 py-3">
				<Card.Header class="px-4 pb-0">
					<Card.Description>{t('app.metrics.avg_gpu')}</Card.Description>
					<Card.Title class="text-2xl tabular-nums">{formatMs(session.avgGpuMs)}</Card.Title>
				</Card.Header>
			</Card.Root>
		</div>

		<div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
			<Card.Root class="gap-1 py-3">
				<Card.Header class="px-4 pb-0">
					<Card.Description>{t('app.metrics.queue_wait_p50')}</Card.Description>
					<Card.Title class="text-2xl tabular-nums">{formatMs(pipeline.queueWaitP50)}</Card.Title>
				</Card.Header>
			</Card.Root>
			<Card.Root class="gap-1 py-3">
				<Card.Header class="px-4 pb-0">
					<Card.Description>{t('app.metrics.queue_wait_p95')}</Card.Description>
					<Card.Title class="text-2xl tabular-nums">{formatMs(pipeline.queueWaitP95)}</Card.Title>
				</Card.Header>
			</Card.Root>
			<Card.Root class="gap-1 py-3">
				<Card.Header class="px-4 pb-0">
					<Card.Description>{t('app.metrics.postprocess_share')}</Card.Description>
					<Card.Title class="text-2xl tabular-nums">
						{pipeline.postprocessSharePct != null
							? `${pipeline.postprocessSharePct.toFixed(0)}%`
							: '-'}
					</Card.Title>
				</Card.Header>
			</Card.Root>
			<Card.Root class="gap-1 py-3">
				<Card.Header class="px-4 pb-0">
					<Card.Description>{t('app.metrics.effective_util')}</Card.Description>
					<Card.Title class="text-2xl tabular-nums">
						{pipeline.effectiveUtilPct != null ? `${pipeline.effectiveUtilPct.toFixed(0)}%` : '-'}
					</Card.Title>
				</Card.Header>
			</Card.Root>
			<Card.Root class="gap-1 py-3">
				<Card.Header class="px-4 pb-0">
					<Card.Description>{t('app.metrics.failure_rate')}</Card.Description>
					<Card.Title class="text-2xl tabular-nums">
						{pipeline.failureRatePct != null ? `${pipeline.failureRatePct.toFixed(0)}%` : '-'}
					</Card.Title>
				</Card.Header>
			</Card.Root>
		</div>

		<Card.Root class="gap-1 py-3">
			<Card.Header class="px-4 pb-0">
				<Card.Description>{t('app.metrics.median_gpu')}</Card.Description>
				<Card.Title class="text-2xl tabular-nums">{formatMs(session.medianGpuMs)}</Card.Title>
			</Card.Header>
		</Card.Root>

		{#if session.byModel.length > 0}
			<StudioSessionModelChart rows={session.byModel} maxGpuMs={maxModelGpu} />
		{/if}

		<section class="flex flex-col gap-2">
			<h3 class="text-sm font-medium">{t('app.metrics.recent')}</h3>
			{#if session.recent.length === 0}
				<p class="text-muted-foreground text-sm">{t('app.metrics.runtime_empty')}</p>
			{:else}
				<div class="border-border overflow-hidden rounded-lg border">
					<table class="w-full min-w-[28rem] text-sm">
						<thead class="bg-muted/30 text-muted-foreground text-left text-xs">
							<tr>
								<th class="px-4 py-2.5 font-medium">{t('app.metrics.col_model')}</th>
								<th class="px-4 py-2.5 font-medium">{t('app.metrics.col_gpu')}</th>
								<th class="px-4 py-2.5 font-medium">{t('app.metrics.col_size')}</th>
							</tr>
						</thead>
						<tbody>
							{#each session.recent as row (row.id)}
								<tr class="border-border/60 border-t">
									<td class="px-4 py-2.5 font-mono text-xs">{row.modelId}</td>
									<td class="px-4 py-2.5 font-mono text-xs tabular-nums">{formatMs(row.gpuMs)}</td>
									<td class="px-4 py-2.5 font-mono text-xs tabular-nums">
										{row.width} x {row.height}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</section>
	{:else if sessionsLoading}
		<p class="text-muted-foreground text-sm">{t('app.metrics.benchmark_loading')}</p>
	{:else if sessionsError || sessions.length === 0}
		<p class="text-muted-foreground text-sm">{t('app.metrics.benchmark_empty')}</p>
	{:else}
		<div class="flex flex-col gap-2">
			<span class="text-muted-foreground text-xs font-medium tracking-wide uppercase">
				{t('app.metrics.benchmark_session')}
			</span>
			<select
				class="border-input bg-background h-9 w-full rounded-lg border px-2.5 text-sm"
				bind:value={selectedSessionId}
			>
				{#each sessions as entry (entry.id)}
					<option value={entry.id}>{entry.label}</option>
				{/each}
			</select>
		</div>

		{#if selectedSession}
			<div class="flex flex-wrap items-center gap-2">
				{#if selectedSession.report.target_vram_gb}
					<Badge variant="secondary">{selectedSession.report.target_vram_gb} GB VRAM</Badge>
				{/if}
				<Badge variant="secondary">
					{selectedSession.report.succeeded}/{selectedSession.report.total_jobs}
					{t('app.metrics.benchmark_images')}
				</Badge>
				<Button href={resolve('/benchmark')} variant="outline" size="sm" class="ms-auto">
					<ExternalLinkIcon />
					{t('app.metrics.open_benchmark_page')}
				</Button>
			</div>

			<div class="border-border overflow-x-auto rounded-lg border">
				<table class="w-full min-w-[32rem] text-sm">
					<thead class="bg-muted/40 text-muted-foreground text-left text-xs uppercase">
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
		{/if}
	{/if}
</div>
