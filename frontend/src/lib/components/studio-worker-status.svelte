<script lang="ts">
	// The sidebar's worker card: is a worker connected, how busy is its GPU,
	// and how many of this account's jobs are waiting. The GPU snapshot comes
	// from /api/v1/studio/gpu, which only an admin may read; everyone else sees
	// the card only while their own jobs are queued or running, and then with
	// nothing but those counts.
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import { account } from '$lib/account.svelte';
	import type { GpuHardware } from '$lib/studio-gpu-sampler';
	import { t } from '$lib/i18n.svelte';
	import { studio } from '$lib/studio.svelte';

	const POLL_MS = 15000;

	// AUTH_MODE=none has no account and runs as the local admin. The static
	// marketing build has no API to ask.
	const mayReadGpu = $derived(
		PUBLIC_SITE_MODE !== 'landing' && (account.current === null || account.current.role === 'admin')
	);
	let gpu = $state<GpuHardware | null>(null);
	// null until the first answer: unknown is not the same as offline.
	let online = $state<boolean | null>(null);

	const queued = $derived(studio.history.filter((g) => g.state === 'queued').length);
	const running = $derived(studio.history.filter((g) => g.state === 'running').length);
	const util = $derived(
		typeof gpu?.util_pct === 'number' ? Math.round(Math.max(0, Math.min(100, gpu.util_pct))) : null
	);
	const vramGb = $derived(
		typeof gpu?.vram_total_bytes === 'number' && gpu.vram_total_bytes > 0
			? Math.round(gpu.vram_total_bytes / 1024 ** 3)
			: null
	);
	const visible = $derived((mayReadGpu && online !== null) || queued + running > 0);

	$effect(() => {
		if (!mayReadGpu) {
			gpu = null;
			online = null;
			return;
		}
		let stopped = false;
		let controller: AbortController | null = null;
		async function poll(): Promise<void> {
			if (document.visibilityState === 'hidden') return;
			controller?.abort();
			controller = new AbortController();
			try {
				const response = await fetch('/api/v1/studio/gpu', { signal: controller.signal });
				if (stopped) return;
				if (response.status === 503) {
					online = false;
					gpu = null;
					return;
				}
				if (!response.ok) return;
				const body = (await response.json()) as { gpu?: GpuHardware };
				if (stopped) return;
				online = true;
				gpu = body.gpu ?? null;
			} catch {
				// Network trouble or an abort: keep the last answer.
			}
		}
		void poll();
		const timer = setInterval(() => void poll(), POLL_MS);
		const onVisible = () => {
			if (document.visibilityState === 'visible') void poll();
		};
		document.addEventListener('visibilitychange', onVisible);
		return () => {
			stopped = true;
			controller?.abort();
			clearInterval(timer);
			document.removeEventListener('visibilitychange', onVisible);
		};
	});
</script>

{#if visible}
	<section
		class="bg-raised border-border mx-2 mb-1 rounded-lg border px-2.5 pt-2.5 pb-2 group-data-[collapsible=icon]:hidden"
		aria-label={t('app.shell.worker_status')}
	>
		<div class="flex items-center gap-2 text-xs font-medium">
			{#if mayReadGpu && online !== null}
				<span
					class={'size-[7px] shrink-0 rounded-full ' +
						(online ? 'bg-status-success' : 'bg-status-error')}
					aria-hidden="true"
				></span>
				<span class="truncate">
					{online ? t('app.shell.worker_online') : t('app.shell.worker_offline')}
				</span>
			{:else}
				<span class="bg-status-running size-[7px] shrink-0 rounded-full" aria-hidden="true"></span>
				<span class="truncate">{t('app.shell.your_jobs')}</span>
			{/if}
			<span class="text-dim ms-auto shrink-0 text-[11px] tabular-nums">
				{t('app.shell.queue_depth').replace('{count}', String(queued))}
			</span>
		</div>
		{#if mayReadGpu && gpu !== null}
			<div class="text-dim mt-2 flex justify-between gap-2 text-[11px]">
				<span class="truncate" title={gpu.device}
					>{gpu.device}{vramGb !== null ? ` · ${vramGb} GB` : ''}</span
				>
				{#if util !== null}
					<span class="shrink-0 tabular-nums">GPU {util}%</span>
				{/if}
			</div>
			{#if util !== null}
				<div
					class="bg-line-strong mt-1.5 h-1 overflow-hidden rounded-full"
					role="meter"
					aria-label={t('app.shell.gpu_load')}
					aria-valuemin={0}
					aria-valuemax={100}
					aria-valuenow={util}
				>
					<div class="bg-foreground h-full rounded-full" style:width={`${util}%`}></div>
				</div>
			{/if}
		{:else if running > 0}
			<p class="text-dim mt-1.5 text-[11px] tabular-nums">
				{t('app.shell.running_count').replace('{count}', String(running))}
			</p>
		{/if}
	</section>
{/if}
