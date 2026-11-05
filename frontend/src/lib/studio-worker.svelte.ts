import { PUBLIC_SITE_MODE } from '$env/static/public';
import { account } from '$lib/account.svelte';
import type { GpuHardware } from '$lib/studio-gpu-sampler';

export const worker = $state({
	online: null as boolean | null,
	gpu: null as GpuHardware | null
});

const POLL_MS = 15000;
let watchers = 0;
let stopWatching: (() => void) | null = null;

export function watchWorker(): () => void {
	watchers += 1;
	if (watchers === 1) {
		stopWatching = $effect.root(() => {
			$effect(() => {
				if (
					PUBLIC_SITE_MODE === 'landing' ||
					(account.current !== null && account.current.role !== 'admin')
				) {
					worker.online = null;
					worker.gpu = null;
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
							worker.online = false;
							worker.gpu = null;
							return;
						}
						if (!response.ok) return;
						const body: { gpu?: GpuHardware | null } = await response.json();
						if (stopped) return;
						worker.online = true;
						worker.gpu = body.gpu ?? null;
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
		});
	}

	let stopped = false;
	return () => {
		if (stopped) return;
		stopped = true;
		watchers -= 1;
		if (watchers !== 0) return;
		stopWatching?.();
		stopWatching = null;
		worker.online = null;
		worker.gpu = null;
	};
}
