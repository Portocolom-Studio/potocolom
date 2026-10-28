<script lang="ts">
	import { onDestroy, onMount, untrack } from 'svelte';
	import { account } from '$lib/account.svelte';
	import { accountCacheKey, studioCache } from '$lib/swr-cache';
	import { t, getLocale } from '$lib/i18n.svelte';
	import { formatMs } from '$lib/benchmark';
	import {
		loadBenchmarkSessionReport,
		loadBenchmarkSessions,
		mergeBenchmarkSessions,
		retainBenchmarkReports,
		type BenchmarkSession
	} from '$lib/studio-benchmark-sessions';
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
	import { demoGpuHistory, demoGpuSamples, demoHistory } from '$lib/studio-demo-metrics';
	import {
		getMetricsPreferences,
		saveMetricsPreferences,
		studio,
		viewOwnerEpoch,
		type Generation
	} from '$lib/studio.svelte';
	import StudioBenchmarkReport from '$lib/components/studio-benchmark-report.svelte';
	import StudioGpuTimelineChart from '$lib/components/studio-gpu-timeline-chart.svelte';
	import StudioJobStatusChart from '$lib/components/studio-job-status-chart.svelte';
	import StudioMetricsMine from '$lib/components/studio-metrics-mine.svelte';
	import StudioSessionModelChart from '$lib/components/studio-session-model-chart.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import * as Select from '$lib/components/ui/select';

	const FAILURES_LIMIT = 20;
	const SESSIONS_KEY = 'metrics:benchmark-sessions';
	const GPU_HISTORY_KEY = 'metrics:gpu-history';
	const ownerEpoch = viewOwnerEpoch();
	const initialPreferences = getMetricsPreferences();
	// Failed jobs are the account's own, so their entry is keyed by account;
	// benchmark sessions and GPU history are the install's.
	const failuresKey = () => accountCacheKey(account.ownerId, 'metrics:failures');

	let liveTick = $state(0);
	// Seeded from the last visit (stale-while-revalidate): the view paints
	// what it showed before and the effects below refresh it.
	const cachedSessions = studioCache.read<BenchmarkSession[]>(SESSIONS_KEY) ?? [];
	let sessions = $state<BenchmarkSession[]>(cachedSessions);
	let sessionsRequested = false;
	let sessionsLoading = $state(false);
	let sessionsError = $state(false);
	let selectedSessionId = $state<string | null>(
		initialPreferences.selectedSessionId ?? cachedSessions[0]?.id ?? null
	);
	let reportErrorId = $state<string | null>(null);
	let metricsRange = $state<MetricsRange>(initialPreferences.usageRange);
	let persistedHistory = $state<GpuHistoryPoint[]>(
		studioCache.read(`${GPU_HISTORY_KEY}:${initialPreferences.usageRange}`) ?? []
	);
	let recentFailures = $state<Generation[]>(studioCache.read(failuresKey()) ?? []);
	let failuresLoading = $state(false);
	let fetchedFailedCount = $state<number | null>(null);
	let failuresInFlight = false;
	let sessionsRetryVersion = $state(0);
	let sessionsController: AbortController | null = null;
	let reportController: AbortController | null = null;
	let failureController: AbortController | null = null;
	let readyReportHeight = $state(0);

	const demoMode = $derived.by(() => {
		void liveTick;
		return (
			studio.history.length === 0 && lastGpuHardware() == null && persistedHistory.length === 0
		);
	});
	const effectiveHistory = $derived.by(() => {
		// liveTick keeps the seeded demo timestamps anchored to the moving
		// window; without it they freeze at first render and drift out of range
		void liveTick;
		return demoMode ? demoHistory() : studio.history;
	});
	const session = $derived(computeSessionMetrics(effectiveHistory));
	const pipeline = $derived(computePipelineMetrics(effectiveHistory));
	const jobStatus = $derived(computeJobStatusBreakdown(effectiveHistory, t));
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
		if (demoMode) {
			return buildGpuTimeline(
				effectiveHistory,
				demoGpuSamples(),
				null,
				metricsRange,
				demoGpuHistory(metricsRange)
			);
		}
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
		const requestEpoch = ownerEpoch;
		const range = metricsRange;
		void liveTick;
		const now = Date.now();
		const from = now - METRICS_RANGE_MS[range];
		let cancelled = false;
		const key = `${GPU_HISTORY_KEY}:${range}`;
		const cached = untrack(() => studioCache.read<GpuHistoryPoint[]>(key));
		persistedHistory = cached ?? [];
		void fetchGpuHistory(from, now, historyRollupForRange(range)).then((points) => {
			if (cancelled || requestEpoch !== viewOwnerEpoch() || points === null) return;
			persistedHistory = points;
			studioCache.write(key, points);
		});
		return () => {
			cancelled = true;
		};
	});

	$effect(() => {
		const tab = studio.metricsTab;
		if (ownerEpoch !== viewOwnerEpoch()) return;
		saveMetricsPreferences({ ...getMetricsPreferences(), tab });
	});

	$effect(() => {
		const range = metricsRange;
		if (ownerEpoch !== viewOwnerEpoch()) return;
		saveMetricsPreferences({ ...getMetricsPreferences(), usageRange: range });
	});

	$effect(() => {
		const selected = selectedSessionId;
		if (ownerEpoch !== viewOwnerEpoch()) return;
		saveMetricsPreferences({ ...getMetricsPreferences(), selectedSessionId: selected });
	});
	const selectedSession = $derived(
		sessions.find((entry) => entry.id === selectedSessionId) ??
			(selectedSessionId === null ? (sessions[0] ?? null) : null)
	);
	const pendingReportId = $derived(
		selectedSession && !selectedSession.report ? selectedSession.id : null
	);
	const latestSample = $derived.by(() => {
		void liveTick;
		const samples = demoMode ? demoGpuSamples() : gpuSamples();
		return samples.length > 0 ? samples[samples.length - 1] : null;
	});

	const displayedFailures = $derived.by(() => {
		void liveTick;
		if (demoMode) {
			return demoHistory()
				.filter((generation) => generation.state === 'failed')
				.slice(0, FAILURES_LIMIT);
		}
		return recentFailures;
	});
	const failedHistoryCount = $derived(
		studio.history.filter((generation) => generation.state === 'failed').length
	);

	function modelName(id: string): string {
		return studio.models.find((model) => model.id === id)?.name ?? id;
	}

	function formatFailureWhen(iso: string | null): string {
		if (!iso) return '-';
		const parsed = Date.parse(iso);
		if (Number.isNaN(parsed)) return iso;
		return new Date(parsed).toLocaleString(getLocale(), {
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	async function loadRecentFailures(): Promise<void> {
		const requestEpoch = ownerEpoch;
		const requestOwnerId = account.ownerId;
		const cacheKey = failuresKey();
		const controller = new AbortController();
		failureController?.abort();
		failureController = controller;
		failuresLoading = true;
		// The failures belong to the account that asked for them: a response
		// that lands after unmount or after another account signs in is dropped.
		const current = () =>
			!controller.signal.aborted &&
			requestEpoch === viewOwnerEpoch() &&
			requestOwnerId === account.ownerId &&
			cacheKey === failuresKey();
		try {
			const response = await fetch(`/api/v1/generations?state=failed&limit=${FAILURES_LIMIT}`, {
				signal: controller.signal
			});
			if (!current()) return;
			if (!response.ok) {
				recentFailures = [];
				return;
			}
			const loaded = (await response.json()) as Generation[];
			if (!current()) return;
			recentFailures = loaded;
			studioCache.write(cacheKey, loaded);
		} catch {
			if (current()) recentFailures = [];
		} finally {
			if (requestEpoch === viewOwnerEpoch() && failureController === controller) {
				failuresLoading = false;
				failureController = null;
			}
		}
	}

	async function refreshBenchmarkSessions(): Promise<void> {
		if (sessionsLoading) return;
		const requestEpoch = ownerEpoch;
		const cacheKey = SESSIONS_KEY;
		const hadSessionsAtStart = untrack(() => sessions.length > 0);
		const controller = new AbortController();
		sessionsController?.abort();
		sessionsController = controller;
		sessionsLoading = true;
		try {
			const loaded = await loadBenchmarkSessions(true, controller.signal, {
				fallbackToStaticOnApiFailure: !hadSessionsAtStart
			});
			if (controller.signal.aborted || requestEpoch !== viewOwnerEpoch()) return;
			sessionsError = loaded.length === 0;
			if (loaded.length === 0) {
				return;
			}
			const currentSessions = untrack(() => sessions);
			const currentSelectedId = untrack(() => selectedSessionId);
			const merged = mergeBenchmarkSessions(loaded, currentSessions, currentSelectedId);
			sessions = merged.sessions;
			if (merged.selectedSessionId !== currentSelectedId) {
				selectedSessionId = merged.selectedSessionId;
				saveMetricsPreferences({
					...getMetricsPreferences(),
					selectedSessionId: merged.selectedSessionId
				});
			}
			studioCache.write(cacheKey, sessions);
		} catch {
			if (!controller.signal.aborted && requestEpoch === viewOwnerEpoch()) sessionsError = true;
		} finally {
			if (requestEpoch === viewOwnerEpoch() && sessionsController === controller) {
				sessionsLoading = false;
				sessionsController = null;
			}
		}
	}

	function retryBenchmarkSessions(): void {
		sessionsError = false;
		sessionsRequested = false;
		sessionsRetryVersion += 1;
	}

	onMount(() => {
		const unsubscribe = subscribeGpuSamples(() => {
			liveTick += 1;
		});
		return () => {
			unsubscribe();
		};
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
		if (studio.metricsTab !== 'usage') {
			fetchedFailedCount = null;
			return;
		}
		if (demoMode) return;
		const count = failedHistoryCount;
		if (failuresInFlight || fetchedFailedCount === count) return;
		fetchedFailedCount = count;
		failuresInFlight = true;
		void loadRecentFailures().finally(() => {
			failuresInFlight = false;
		});
	});

	$effect(() => {
		void sessionsRetryVersion;
		if (studio.metricsTab !== 'benchmarks') {
			sessionsRequested = false;
			return;
		}
		if (sessionsRequested) return;
		// Cached sessions paint immediately while the refreshed list arrives.
		sessionsRequested = true;
		void refreshBenchmarkSessions();
	});

	$effect(() => {
		const tab = studio.metricsTab;
		const id = pendingReportId;
		const errorId = reportErrorId;
		if (tab !== 'benchmarks' || id === null || errorId === id) {
			return;
		}
		const requestEpoch = ownerEpoch;
		const cacheKey = SESSIONS_KEY;
		const controller = new AbortController();
		reportController?.abort();
		reportController = controller;
		void loadBenchmarkSessionReport(id, controller.signal)
			.then((report) => {
				if (controller.signal.aborted || requestEpoch !== viewOwnerEpoch()) return;
				if (report === null) {
					reportErrorId = id;
					return;
				}
				const currentSessions = untrack(() => sessions);
				const currentSelectedId = untrack(() => selectedSessionId);
				const updated = currentSessions.map((entry) =>
					entry.id === id ? { ...entry, report } : entry
				);
				sessions = retainBenchmarkReports(updated, currentSelectedId);
				studioCache.write(cacheKey, sessions);
			})
			.catch(() => {
				if (!controller.signal.aborted && requestEpoch === viewOwnerEpoch()) reportErrorId = id;
			})
			.finally(() => {
				if (requestEpoch === viewOwnerEpoch() && reportController === controller) {
					reportController = null;
				}
			});
		return () => {
			controller.abort();
			if (reportController === controller) {
				reportController = null;
			}
		};
	});

	onDestroy(() => {
		sessionsController?.abort();
		reportController?.abort();
		failureController?.abort();
	});
</script>

{#snippet stat(label: string, value: string, sub?: string)}
	<div class="bg-card border-border rounded-[10px] border px-4 py-3.5">
		<p class="text-muted-foreground text-xs">{label}</p>
		<p class="mt-1.5 text-[22px] leading-tight font-medium tabular-nums">{value}</p>
		{#if sub}
			<p class="text-dim mt-1 text-[11.5px] tabular-nums">{sub}</p>
		{/if}
	</div>
{/snippet}

{#snippet loadingBlock(label: string, rows: number)}
	<div role="status" class="flex flex-col gap-2">
		<span class="sr-only">{label}</span>
		<div class="bg-card border-border rounded-[10px] border" aria-hidden="true">
			{#each Array.from({ length: rows }, (_, index) => index) as index (index)}
				<div class="border-border flex h-10 items-center gap-4 px-4 not-first:border-t">
					<Skeleton class="h-3 w-24 rounded" />
					<Skeleton class="h-3 w-32 rounded" />
					<Skeleton class="h-3 flex-1 rounded" />
				</div>
			{/each}
		</div>
	</div>
{/snippet}

<div class="flex flex-col gap-3">
	{#if studio.metricsTab === 'usage' && !studio.historyLoaded}
		<!-- The usage numbers come from the history; until its first page
		     arrives the demo data would flash in, so show the layout instead. -->
		<div role="status" class="flex flex-col gap-3">
			<span class="sr-only">{t('app.metrics.loading')}</span>
			<div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-hidden="true">
				{#each [0, 1, 2, 3] as index (index)}
					<Skeleton class="h-[86px] rounded-[10px]" />
				{/each}
			</div>
			<div class="grid gap-3 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]" aria-hidden="true">
				<Skeleton class="h-72 rounded-[10px]" />
				<Skeleton class="h-72 rounded-[10px]" />
			</div>
		</div>
	{:else if studio.metricsTab === 'usage'}
		{#if demoMode}
			<span class="tag tag-warning w-fit">{t('app.metrics.demo_data')}</span>
		{/if}
		<div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
			{@render stat(
				t('app.metrics.images_count'),
				String(session.count),
				t('app.metrics.median_per_job').replace('{value}', formatMs(session.medianGpuMs))
			)}
			{@render stat(t('app.metrics.total_gpu'), formatMs(session.totalGpuMs || null))}
			{@render stat(
				t('app.metrics.queue_wait_p95'),
				formatMs(pipeline.queueWaitP95),
				`p50 ${formatMs(pipeline.queueWaitP50)}`
			)}
			{@render stat(
				t('app.metrics.failure_rate'),
				pipeline.failureRatePct != null ? `${pipeline.failureRatePct.toFixed(1)}%` : '-'
			)}
		</div>

		{#if latestSample?.hardwareAvailable}
			<div class="flex flex-wrap gap-2">
				{#if latestSample.vramUsedPct != null}
					<span class="tag">VRAM {latestSample.vramUsedPct}%</span>
				{/if}
				{#if latestSample.temperatureC != null}
					<span class="tag">{latestSample.temperatureC} C</span>
				{/if}
				{#if latestSample.powerW != null}
					<span class="tag">{latestSample.powerW.toFixed(0)} W</span>
				{/if}
			</div>
		{/if}

		<div class="grid gap-3 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
			<div class="min-w-0">
				<StudioGpuTimelineChart
					{timeline}
					vramAvailable={demoMode || hardwareVramPct != null}
					bind:range={metricsRange}
					live={gpuSamplerLive && !demoMode}
				/>
			</div>
			<StudioJobStatusChart breakdown={jobStatus} />
		</div>

		<div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
			{@render stat(t('app.metrics.avg_gpu'), formatMs(session.avgGpuMs))}
			{@render stat(
				t('app.metrics.postprocess_share'),
				pipeline.postprocessSharePct != null ? `${pipeline.postprocessSharePct.toFixed(0)}%` : '-'
			)}
			{@render stat(
				t('app.metrics.in_job_gpu_share'),
				pipeline.inJobGpuSharePct != null ? `${pipeline.inJobGpuSharePct.toFixed(0)}%` : '-'
			)}
			{@render stat(
				t('app.metrics.session_util'),
				pipeline.sessionUtilPct != null ? `${pipeline.sessionUtilPct.toFixed(0)}%` : '-'
			)}
		</div>

		{#if session.byModel.length > 0}
			<StudioSessionModelChart rows={session.byModel} />
		{/if}

		<section class="flex flex-col gap-2">
			<h3 class="text-[13px] font-semibold">{t('app.metrics.recent')}</h3>
			{#if session.recent.length === 0}
				<p class="text-muted-foreground text-sm">{t('app.metrics.runtime_empty')}</p>
			{:else}
				<div class="bg-card border-border overflow-x-auto rounded-[10px] border">
					<table class="w-full min-w-[28rem] text-sm">
						<thead class="text-muted-foreground border-border border-b text-left text-xs">
							<tr>
								<th class="h-[34px] px-4 font-medium">{t('app.metrics.col_model')}</th>
								<th class="h-[34px] px-4 font-medium">{t('app.metrics.col_gpu')}</th>
								<th class="h-[34px] px-4 font-medium">{t('app.metrics.col_size')}</th>
							</tr>
						</thead>
						<tbody>
							{#each session.recent as row (row.id)}
								<tr class="border-border border-t first:border-t-0">
									<td class="px-4 py-3 text-[12.5px]">{modelName(row.modelId)}</td>
									<td class="px-4 py-3 text-[12.5px] tabular-nums">{formatMs(row.gpuMs)}</td>
									<td class="px-4 py-3 text-[12.5px] tabular-nums">
										{row.width} x {row.height}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</section>

		<section class="flex flex-col gap-2">
			<h3 class="text-[13px] font-semibold">{t('app.metrics.failures')}</h3>
			{#if failuresLoading && displayedFailures.length === 0}
				{@render loadingBlock(t('app.metrics.failures_loading'), 3)}
			{:else if displayedFailures.length === 0}
				<p class="text-muted-foreground text-sm">{t('app.metrics.failures_empty')}</p>
			{:else}
				<div class="bg-card border-border overflow-x-auto rounded-[10px] border">
					<table class="w-full min-w-[32rem] text-sm">
						<thead class="text-muted-foreground border-border border-b text-left text-xs">
							<tr>
								<th class="h-[34px] px-4 font-medium">{t('app.metrics.col_when')}</th>
								<th class="h-[34px] px-4 font-medium">{t('app.metrics.col_model')}</th>
								<th class="h-[34px] px-4 font-medium">{t('app.metrics.col_reason')}</th>
							</tr>
						</thead>
						<tbody>
							{#each displayedFailures as row (row.id)}
								<tr class="border-border border-t align-top first:border-t-0">
									<td class="text-muted-foreground px-4 py-2.5 text-xs whitespace-nowrap">
										{formatFailureWhen(row.finished_at ?? row.created_at)}
									</td>
									<td class="px-4 py-3 text-[12.5px]">{modelName(row.model_id)}</td>
									<td class="px-4 py-3 text-[12.5px] wrap-break-word whitespace-pre-wrap">
										{row.failure_reason ?? '-'}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</section>
	{:else if studio.metricsTab === 'mine'}
		<StudioMetricsMine />
	{:else if sessionsLoading && selectedSession === null}
		{@render loadingBlock(t('app.metrics.benchmark_loading'), 5)}
	{:else if sessions.length === 0}
		{#if sessionsError}
			<div class="flex items-center gap-3">
				<p class="text-muted-foreground text-sm">{t('app.metrics.benchmark_error')}</p>
				<Button variant="outline" size="sm" onclick={retryBenchmarkSessions}>
					{t('app.metrics.benchmark_retry')}
				</Button>
			</div>
		{:else}
			<p class="text-muted-foreground text-sm">{t('app.metrics.benchmark_empty')}</p>
		{/if}
	{:else}
		{#if sessionsError}
			<div class="flex items-center gap-3" role="status">
				<p class="text-muted-foreground text-sm">{t('app.metrics.benchmark_error')}</p>
				<Button variant="outline" size="sm" onclick={retryBenchmarkSessions}>
					{t('app.metrics.benchmark_retry')}
				</Button>
			</div>
		{/if}
		<div class="flex flex-col gap-2">
			<span class="text-muted-foreground text-xs font-medium tracking-wide uppercase">
				{t('app.metrics.benchmark_session')}
			</span>
			<Select.Root
				type="single"
				value={selectedSessionId ?? undefined}
				onValueChange={(next) => {
					if (next) {
						selectedSessionId = next;
						reportErrorId = null;
					}
				}}
			>
				<Select.Trigger class="w-full">
					{selectedSession?.label ?? t('app.metrics.benchmark_session')}
				</Select.Trigger>
				<Select.Content>
					<Select.Group>
						{#each sessions as entry (entry.id)}
							<Select.Item value={entry.id} label={entry.label} />
						{/each}
					</Select.Group>
				</Select.Content>
			</Select.Root>
		</div>

		{#if selectedSession?.report}
			{#key selectedSession.id}
				<StudioBenchmarkReport
					sessionId={selectedSession.id}
					report={selectedSession.report}
					bind:clientHeight={readyReportHeight}
				/>
			{/key}
		{:else if reportErrorId === selectedSession?.id}
			<div class="flex items-center gap-3">
				<p class="text-muted-foreground text-sm">{t('app.metrics.benchmark_error')}</p>
				<Button variant="outline" size="sm" onclick={() => (reportErrorId = null)}>
					{t('app.metrics.benchmark_retry')}
				</Button>
			</div>
		{:else if pendingReportId === selectedSession?.id}
			<div
				data-benchmark-loading-report={selectedSession.id}
				style:min-height={readyReportHeight > 0 ? `min(${readyReportHeight}px, 100vh)` : undefined}
			>
				{@render loadingBlock(t('app.metrics.benchmark_loading'), 5)}
			</div>
		{:else}
			<p class="text-muted-foreground text-sm">{t('app.metrics.benchmark_empty')}</p>
		{/if}
	{/if}
</div>
