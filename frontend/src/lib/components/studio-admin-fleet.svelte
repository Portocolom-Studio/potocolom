<script lang="ts">
	import { apiFetch } from '$lib/api';
	import {
		gpuHistoryPoints,
		gpuHistorySearch,
		historyRollupForRange,
		type GpuHistoryResponse,
		type GpuHistoryPoint
	} from '$lib/studio-gpu-history';
	import { buildGpuTimeline } from '$lib/studio-gpu-timeline';
	import { type MetricsRange, METRICS_RANGE_MS } from '$lib/studio-metrics-range';
	import { adminErrorMessage, recheckAdminAccess } from '$lib/studio-admin-logic';
	import { t } from '$lib/i18n.svelte';
	import StudioGpuTimelineChart from '$lib/components/studio-gpu-timeline-chart.svelte';
	import * as Card from '$lib/components/ui/card';

	let range = $state<MetricsRange>('1h');
	let workers = $state<string[]>([]);
	let selectedWorker = $state('');
	let samples = $state<GpuHistoryPoint[]>([]);
	let workerLoading = $state(true);
	let historyLoading = $state(false);
	let historyError = $state('');
	let workerLoadEpoch = 0;
	let discoveryEpoch = 0;

	const timeline = $derived(buildGpuTimeline([], [], null, range, samples));
	const vramAvailable = $derived(samples.some((sample) => sample.vram_used_pct !== null));

	async function apiError(response: Response): Promise<string> {
		if (response.status === 403) void recheckAdminAccess();
		const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
		return adminErrorMessage(body?.detail, response.statusText);
	}

	async function requestHistory(workerId?: string): Promise<Response> {
		const now = Date.now();
		const from = now - METRICS_RANGE_MS[range];
		const search = gpuHistorySearch(from, now, historyRollupForRange(range), workerId);
		return apiFetch(`/api/v1/metrics/gpu/history${search}`);
	}

	async function discoverWorkers(cancelled: () => boolean): Promise<void> {
		const epoch = ++discoveryEpoch;
		workerLoading = true;
		historyError = '';
		try {
			const response = await requestHistory();
			if (cancelled() || epoch !== discoveryEpoch) return;
			if (!response.ok) {
				historyError = await apiError(response);
				workers = [];
				selectedWorker = '';
				return;
			}
			const points = gpuHistoryPoints((await response.json()) as GpuHistoryResponse);
			const nextWorkers = [...new Set(points.flatMap((point) => point.worker_id ?? []))].sort();
			workers = nextWorkers;
			if (!nextWorkers.includes(selectedWorker)) selectedWorker = nextWorkers[0] ?? '';
		} catch {
			if (!cancelled() && epoch === discoveryEpoch) {
				historyError = t('app.admin.request_failed');
				workers = [];
				selectedWorker = '';
			}
		} finally {
			if (!cancelled() && epoch === discoveryEpoch) workerLoading = false;
		}
	}

	async function loadSelectedWorker(workerId: string): Promise<void> {
		const epoch = ++workerLoadEpoch;
		historyError = '';
		if (workerId === '') {
			samples = [];
			historyLoading = false;
			return;
		}
		historyLoading = true;
		try {
			const response = await requestHistory(workerId);
			if (epoch !== workerLoadEpoch) return;
			if (!response.ok) {
				historyError = await apiError(response);
				samples = [];
				return;
			}
			samples = gpuHistoryPoints((await response.json()) as GpuHistoryResponse);
		} catch {
			if (epoch === workerLoadEpoch) {
				historyError = t('app.admin.request_failed');
				samples = [];
			}
		} finally {
			if (epoch === workerLoadEpoch) historyLoading = false;
		}
	}

	$effect(() => {
		const activeRange = range;
		let cancelled = false;
		void activeRange;
		void discoverWorkers(() => cancelled);
		return () => {
			cancelled = true;
		};
	});

	$effect(() => {
		const workerId = selectedWorker;
		const activeRange = range;
		void activeRange;
		void loadSelectedWorker(workerId);
	});
</script>

<div class="flex min-h-0 flex-col gap-4 overflow-auto pb-2">
	<Card.Root class="p-0 [--card-spacing:0]">
		<Card.Header class="border-border border-b px-4 py-3">
			<div class="flex flex-wrap items-center justify-between gap-3">
				<div>
					<Card.Title class="text-base">{t('app.admin.fleet_history')}</Card.Title>
					<Card.Description>{t('app.admin.fleet_history_sub')}</Card.Description>
				</div>
				<label class="flex min-w-52 flex-col gap-1 text-xs font-medium">
					{t('app.admin.worker')}
					<select
						data-testid="admin-worker-select"
						class="border-input bg-background h-9 rounded-md border px-2 text-sm"
						bind:value={selectedWorker}
						disabled={workerLoading || workers.length === 0}
					>
						{#each workers as worker (worker)}
							<option value={worker}>{worker}</option>
						{/each}
					</select>
				</label>
			</div>
		</Card.Header>
		<Card.Content class="flex flex-col gap-3 p-4">
			<div class="text-muted-foreground flex flex-wrap gap-x-5 gap-y-1 text-xs">
				<span data-testid="admin-fleet-worker-count">
					{t('app.admin.workers_count').replace('{count}', String(workers.length))}
				</span>
				<span data-testid="admin-fleet-sample-count">
					{t('app.admin.samples_count').replace('{count}', String(samples.length))}
				</span>
			</div>
			{#if historyError}
				<p role="alert" class="text-destructive text-sm">{historyError}</p>
			{:else if workerLoading || historyLoading}
				<p role="status" class="text-muted-foreground text-sm">
					{t('app.admin.loading_fleet')}
				</p>
			{:else if workers.length === 0}
				<p class="text-muted-foreground text-sm">{t('app.admin.workers_empty')}</p>
			{:else}
				<StudioGpuTimelineChart
					{timeline}
					{vramAvailable}
					bind:range
					live={false}
					showModelActivity={false}
					metricEmptyHint={t('app.admin.gpu_history_empty')}
					hintText={t('app.admin.gpu_history_hint')}
				/>
			{/if}
		</Card.Content>
	</Card.Root>
</div>
