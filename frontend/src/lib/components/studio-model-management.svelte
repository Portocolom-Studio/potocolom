<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { apiFetch } from '$lib/api';
	import { recheckAdminAccess } from '$lib/studio-admin-logic';
	import { getLocale, t } from '$lib/i18n.svelte';
	import {
		formatSize as formatLocalizedSize,
		selectionSpace,
		shouldPoll,
		pollInterval,
		initialSelection,
		diskPreview,
		knownSize,
		remainingReservation,
		sumKnown
	} from '$lib/model-management';
	import { Button } from '$lib/components/ui/button';
	import { HardDrive, MemoryStick, Check, Download, AlertTriangle } from '@lucide/svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { GIB } from '$lib/model-management';

	type Download = {
		kind: 'install' | 'remove' | 'cancel';
		state: string;
		bytes_done: number;
		bytes_total: number;
		error: string | null;
		op_id: string | null;
	};
	type Model = {
		id: string;
		name: string;
		capabilities: string[];
		tier: string;
		license_id: string;
		min_vram_gb: number;
		installed: boolean;
		download_bytes: number | null;
		needs_token: boolean;
		fits: boolean;
		suggested: boolean;
		download: Download | null;
	};
	type Worker = {
		worker_id: string;
		manageable: boolean;
		hardware: {
			gpu_name: string;
			vram_total_bytes: number | null;
			disk_total_bytes: number;
			disk_free_bytes: number;
		} | null;
		disk_free_bytes: number | null;
		models: Model[];
	};
	let workers = $state<Worker[]>([]);
	let selected = $state<Record<string, string[]>>({});
	let initialized = new Set<string>();
	let loading = $state(true);
	let error = $state(false);
	let busy = $state(false);
	let alive = false;
	let timer: ReturnType<typeof setTimeout> | undefined;
	type Pending = {
		action: 'install' | 'cancel' | 'remove';
		opId: string;
	};
	let pending = $state<Record<string, Pending>>({});
	let actionErrors = $state<Record<string, string>>({});
	let announcement = $state('');
	let preview = $state<{ worker: string; model: string } | null>(null);
	let refreshVersion = 0;
	let root: HTMLDivElement;
	const formatSize = (bytes: number) => formatLocalizedSize(bytes, getLocale());
	const key = (worker: string, model: string) => `${worker}\u0000${model}`;
	const actionKey = (worker: string, model: string, action: string) =>
		`${key(worker, model)}\u0000${action}`;
	const answers = (download: Download | null, operation: Pending) =>
		download?.op_id != null && download.op_id >= operation.opId;
	const active = (model: Model) => shouldPoll([model.download?.state ?? '']);
	function reserved(worker: Worker) {
		return sumKnown(
			worker.models.flatMap((model) => {
				const operation = pending[key(worker.worker_id, model.id)];
				const current = active(model) && model.download?.kind === 'install';
				if (!current && operation?.action !== 'install') return [];
				if (current && model.download && (!operation || answers(model.download, operation)))
					return [
						remainingReservation(
							model.download.bytes_total,
							model.download_bytes,
							model.download.bytes_done
						)
					];
				return [knownSize(model.download_bytes)];
			})
		);
	}
	function capabilityLabel(capability: string) {
		switch (capability) {
			case 'text_to_image':
				return t('app.models.cap_text_to_image');
			case 'image_to_image':
				return t('app.models.cap_image_to_image');
			case 'upscale':
				return t('app.models.cap_upscale');
			case 'realtime':
				return t('app.models.cap_realtime');
			default:
				return capability;
		}
	}
	function tierLabel(tier: string) {
		switch (tier) {
			case 'draft':
				return t('app.model_management.tier_draft');
			case 'standard':
				return t('app.model_management.tier_standard');
			case 'premium':
				return t('app.model_management.tier_premium');
			default:
				return tier;
		}
	}
	async function retryRefresh() {
		const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		await refresh();
		await recoverFocus(focused);
	}

	function schedule() {
		clearTimeout(timer);
		const interval = pollInterval(
			alive,
			Object.keys(pending).length > 0 || workers.some((w) => w.models.some(active))
		);
		if (interval !== null) timer = setTimeout(() => void refresh(), interval);
	}
	async function refresh() {
		if (!alive) return;
		clearTimeout(timer);
		const version = ++refreshVersion;
		try {
			const response = await apiFetch('/api/v1/admin/models');
			if (response.status === 403) void recheckAdminAccess();
			if (!response.ok) throw new Error('models');
			const data: Worker[] = await response.json();
			if (!alive || version !== refreshVersion) return;
			const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
			const row = focused?.closest<HTMLElement>('[data-model]');
			const workerId = focused?.closest<HTMLElement>('[data-worker]')?.dataset.worker;
			const modelId = row?.dataset.model;
			const focusedHere = !!focused && root.contains(focused);
			const oldWorkers = workers;
			workers = data;
			const present = new Set<string>();
			for (const worker of data) {
				if (worker.manageable && worker.models.length > 0 && !initialized.has(worker.worker_id)) {
					initialized.add(worker.worker_id);
					selected[worker.worker_id] = initialSelection(worker.models);
				}
				if (worker.manageable)
					selected[worker.worker_id] = (selected[worker.worker_id] ?? []).filter((id) =>
						worker.models.some((m) => m.id === id && !m.installed && !active(m))
					);
				for (const model of worker.models) {
					const id = key(worker.worker_id, model.id);
					present.add(id);
					const operation = pending[id];
					const old = oldWorkers
						.find((w) => w.worker_id === worker.worker_id)
						?.models.find((m) => m.id === model.id);
					if (
						operation &&
						answers(model.download, operation) &&
						((model.download?.op_id != null && model.download.op_id > operation.opId) ||
							!active(model))
					) {
						if (
							operation.action === 'remove' &&
							model.download?.op_id === operation.opId &&
							model.download.kind === 'remove'
						) {
							if (model.download?.error === 'not_running') {
								announcement = t('app.model_management.announce_not_running').replace(
									'{model}',
									model.name
								);
							} else if (model.download?.state === 'failed') {
								actionErrors[actionKey(worker.worker_id, model.id, 'remove')] =
									model.download.error ?? t('app.model_management.removal_failed');
								announcement = t('app.model_management.announce_removal_failed').replace(
									'{model}',
									model.name
								);
							} else {
								announcement = t('app.model_management.announce_removed').replace(
									'{model}',
									model.name
								);
							}
						}
						delete pending[id];
					}
					if (
						old &&
						operation?.action !== 'remove' &&
						old.download?.state !== model.download?.state
					) {
						const state = model.download?.state;
						if (state === 'queued' || state === 'downloading')
							announcement = t('app.model_management.announce_started').replace(
								'{model}',
								model.name
							);
						else if (state === 'done')
							announcement = t('app.model_management.announce_finished').replace(
								'{model}',
								model.name
							);
						else if (state === 'cancelled')
							announcement = t('app.model_management.announce_cancelled').replace(
								'{model}',
								model.name
							);
						else if (state === 'failed' && model.download?.error === 'not_running')
							announcement = t('app.model_management.announce_not_running').replace(
								'{model}',
								model.name
							);
						else if (state === 'failed')
							announcement = t(
								model.download?.error === 'cancelled'
									? 'app.model_management.announce_cancelled'
									: 'app.model_management.announce_failed'
							).replace('{model}', model.name);
					}
				}
			}
			for (const id of Object.keys(pending)) if (!present.has(id)) delete pending[id];
			for (const id of Object.keys(selected))
				if (!data.some((w) => w.worker_id === id)) {
					delete selected[id];
				}
			error = false;
			await tick();
			if (!alive || version !== refreshVersion) return;
			if (focusedHere) await recoverFocus(focused, workerId, modelId);
		} catch {
			if (alive && version === refreshVersion) error = true;
		} finally {
			if (alive && version === refreshVersion) {
				loading = false;
				schedule();
			}
		}
	}

	function footerSpace(footer: HTMLElement) {
		const scroller = footer.closest<HTMLElement>('[data-model-scroller]');
		if (!scroller) return;
		const update = () => {
			const height = Math.max(
				0,
				...[...scroller.querySelectorAll<HTMLElement>('[data-selection-footer]')].map(
					(el) => el.offsetHeight
				)
			);
			if (document.activeElement instanceof HTMLElement) revealFocus(document.activeElement);
			scroller.style.scrollPaddingBottom = `${height + 8}px`;
		};
		const observer = new ResizeObserver(update);
		observer.observe(footer);
		return {
			destroy() {
				observer.disconnect();
				requestAnimationFrame(update);
			}
		};
	}
	function revealFocus(target: EventTarget | null) {
		if (!(target instanceof HTMLElement)) return;
		const footer = target
			.closest('[data-worker]')
			?.querySelector<HTMLElement>('[data-selection-footer]');
		const scroller = target.closest<HTMLElement>('[data-model-scroller]');
		if (!footer || !scroller || footer.contains(target)) return;
		const rect = target.getBoundingClientRect();
		const overlay = footer.getBoundingClientRect();
		if (
			rect.bottom > overlay.top &&
			rect.top < overlay.bottom &&
			rect.right > overlay.left &&
			rect.left < overlay.right
		)
			scroller.scrollBy({ top: rect.bottom - overlay.top + 8 });
	}
	function toggle(worker: string, id: string) {
		const ids = selected[worker] ?? [];
		selected[worker] = ids.includes(id) ? ids.filter((item) => item !== id) : [...ids, id];
	}
	async function command(worker: Worker, model: Model, action: 'install' | 'cancel' | 'remove') {
		const path = `/api/v1/admin/workers/${encodeURIComponent(worker.worker_id)}/models/${encodeURIComponent(model.id)}`;
		delete actionErrors[actionKey(worker.worker_id, model.id, action)];
		const response = await apiFetch(path + (action === 'remove' ? '' : `/${action}`), {
			method: action === 'remove' ? 'DELETE' : 'POST'
		});
		if (response.status === 403) void recheckAdminAccess();
		if (!response.ok) {
			const body = await response.json().catch(() => null);
			throw new Error(`${response.status}${body?.detail ? `: ${body.detail}` : ''}`);
		}
		const body: { op_id: string } = await response.json();
		pending[key(worker.worker_id, model.id)] = {
			action,
			opId: body.op_id
		};
		if (action === 'install')
			announcement = t('app.model_management.announce_started').replace('{model}', model.name);
	}
	async function recoverFocus(focused: HTMLElement | null, workerId?: string, modelId?: string) {
		await tick();
		if (!focused) return;
		if (focused.isConnected && !focused.matches(':disabled')) {
			if (document.activeElement === document.body) focused.focus();
		} else if (document.activeElement === document.body || document.activeElement === focused) {
			const worker = [...root.querySelectorAll<HTMLElement>('[data-worker]')].find(
				(el) => el.dataset.worker === workerId
			);
			const model = [...(worker?.querySelectorAll<HTMLElement>('[data-model]') ?? [])].find(
				(el) => el.dataset.model === modelId
			);
			(
				model?.querySelector<HTMLElement>('[data-heading]') ??
				worker?.querySelector<HTMLElement>('h2') ??
				root
			).focus();
		}
		if (document.activeElement instanceof HTMLElement) revealFocus(document.activeElement);
	}
	async function dismiss(worker: Worker, model: Model, action: string) {
		const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		delete actionErrors[actionKey(worker.worker_id, model.id, action)];
		await recoverFocus(focused, worker.worker_id, model.id);
	}
	async function install(worker: Worker, only?: Model) {
		const models = worker.models.filter(
			(m) =>
				(only ? m.id === only.id : (selected[worker.worker_id] ?? []).includes(m.id)) &&
				!m.installed &&
				!m.needs_token &&
				!m.download?.state?.match(/^(queued|downloading)$/) &&
				!pending[key(worker.worker_id, m.id)]
		);
		const free = worker.disk_free_bytes ?? worker.hardware?.disk_free_bytes;
		if (
			free == null ||
			!selectionSpace(
				free,
				models.map((m) => m.download_bytes),
				reserved(worker)
			).allowed
		)
			return;
		const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		preview = null;
		busy = true;
		try {
			for (const model of models) {
				await command(worker, model, 'install');
				selected[worker.worker_id] = (selected[worker.worker_id] ?? []).filter(
					(id) => id !== model.id
				);
			}
		} catch (cause) {
			const model = models.find((m) =>
				only ? m.id === only.id : (selected[worker.worker_id] ?? []).includes(m.id)
			);
			if (model)
				actionErrors[actionKey(worker.worker_id, model.id, 'install')] =
					cause instanceof Error ? cause.message : String(cause);
		} finally {
			busy = false;
			clearTimeout(timer);
			// The started install removes the focused button. Move focus now, not
			// after the refresh request, so focus never rests on the page body.
			if (models[0]) await recoverFocus(focused, worker.worker_id, models[0].id);
			await refresh();
			if (models[0]) await recoverFocus(focused, worker.worker_id, models[0].id);
		}
	}
	async function change(worker: Worker, model: Model, action: 'cancel' | 'remove') {
		// An authoritative reclaimable size field can be shown here when available.
		if (
			action === 'remove' &&
			!confirm(t('app.model_management.remove_confirm').replace('{model}', model.name))
		)
			return;
		const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		busy = true;
		try {
			await command(worker, model, action);
		} catch (cause) {
			actionErrors[actionKey(worker.worker_id, model.id, action)] =
				cause instanceof Error ? cause.message : String(cause);
		} finally {
			busy = false;
			clearTimeout(timer);
			await refresh();
			await recoverFocus(focused, worker.worker_id, model.id);
		}
	}
	onMount(() => {
		alive = true;
		void refresh();
		return () => {
			alive = false;
			clearTimeout(timer);
		};
	});
</script>

<div
	bind:this={root}
	class="space-y-8"
	data-model-list
	tabindex="-1"
	onfocusin={(event) => revealFocus(event.target)}
>
	{#if loading}<p role="status">{t('app.model_management.loading')}</p>{/if}
	{#if error}<p role="alert">{t('app.model_management.error')}</p>
		<Button variant="outline" onclick={() => retryRefresh()}
			>{t('app.model_management.retry')}</Button
		>{/if}
	{#if !loading && !error && workers.length === 0}
		<div class="bg-card border-border text-muted-foreground rounded-xl border p-6 text-sm">
			<p>{t('app.model_management.empty')}</p>
			<a
				href="https://github.com/Portocolom-Studio/potocolom/blob/main/docs/self-hosting.md"
				target="_blank"
				rel="noopener noreferrer"
				class="text-foreground mt-3 inline-block rounded-sm underline underline-offset-4 focus-visible:ring-2 focus-visible:ring-ring"
				>{t('app.gen.setup_guide')}</a
			>
		</div>
	{/if}
	<p class="sr-only" aria-live="polite">{announcement}</p>
	{#each workers as worker (worker.worker_id)}
		<section class="space-y-4" aria-label={worker.worker_id} data-worker={worker.worker_id}>
			<h2 tabindex="-1" class="text-lg font-semibold">{worker.worker_id}</h2>
			{#if !worker.manageable}<p>{t('app.model_management.update')}</p>
			{:else}
				{@const picks = worker.models.filter(
					(m) =>
						(selected[worker.worker_id] ?? []).includes(m.id) &&
						!m.installed &&
						!m.needs_token &&
						m.download?.state !== 'queued' &&
						m.download?.state !== 'downloading' &&
						!pending[key(worker.worker_id, m.id)]
				)}
				{@const free = worker.disk_free_bytes ?? worker.hardware?.disk_free_bytes}
				{@const space =
					free == null
						? null
						: selectionSpace(
								free,
								picks.map((m) => m.download_bytes),
								reserved(worker)
							)}
				{#if worker.hardware}
					{@const free = worker.disk_free_bytes ?? worker.hardware.disk_free_bytes}
					<p>
						{t('app.model_management.hardware')
							.replace('{gpu}', worker.hardware.gpu_name)
							.replace(
								'{vram}',
								!knownSize(worker.hardware.vram_total_bytes)
									? t('app.model_management.unknown')
									: formatSize(worker.hardware?.vram_total_bytes ?? 0)
							)}
					</p>
					{@const total = worker.hardware.disk_total_bytes}
					{#if free != null && total > 0}
						{@const previewModel =
							preview?.worker === worker.worker_id
								? worker.models.find(
										(m) =>
											m.id === preview?.model &&
											!m.installed &&
											!active(m) &&
											!pending[key(worker.worker_id, m.id)]
									)
								: undefined}
						{@const previewSizes = previewModel
							? [previewModel.download_bytes]
							: picks.map((m) => m.download_bytes)}
						{@const previewSpace = previewSizes.length
							? selectionSpace(free, previewSizes, reserved(worker))
							: null}
						{@const known = previewSizes.every((size) => knownSize(size) != null)}
						{@const segments = diskPreview(
							total,
							free,
							reserved(worker) ?? 0,
							previewSpace?.total ?? 0
						)}
						{@const used = segments.used}
						{@const percent = total > 0 ? Math.round((used / total) * 100) : 0}
						<div class="space-y-3 rounded-lg border bg-card p-4" data-disk>
							<div class="flex justify-between gap-3 text-sm">
								<strong>{t('app.model_management.disk_usage')}</strong><span
									>{new Intl.NumberFormat(getLocale()).format(percent)}% · {formatSize(total)}</span
								>
							</div>
							<div
								class="disk-track"
								role="meter"
								aria-label={t('app.model_management.disk_usage')}
								aria-valuemin="0"
								aria-valuenow={used}
								aria-valuemax={total || 1}
								aria-valuetext={`${new Intl.NumberFormat(getLocale()).format(percent)}% · ${t('app.model_management.used')}: ${formatSize(used)} · ${t('app.model_management.free')}: ${formatSize(free)}`}
							>
								<div class="disk-used" style:width={`${(used / (total || 1)) * 100}%`}></div>
								{#if segments.reserved > 0}<div
										class="disk-reserved"
										data-reserved
										style:width={`${(segments.reserved / (total || 1)) * 100}%`}
									></div>{/if}
								{#if previewSpace && known && reserved(worker) !== null}<div
										class="disk-preview"
										class:over={!previewSpace.allowed}
										style:width={`${(segments.selected / (total || 1)) * 100}%`}
									></div>{/if}
							</div>
							<div class="flex flex-wrap gap-x-5 gap-y-1 text-sm text-muted-foreground">
								<span
									><span class="disk-swatch disk-used"></span>{t('app.model_management.used')}: {formatSize(
										used
									)}</span
								>
								<span
									><span class="disk-swatch disk-free"></span>{t('app.model_management.free')}: {formatSize(
										free
									)}</span
								>
								{#if reserved(worker) !== 0}<span
										><span class="disk-swatch disk-reserved"></span>{t(
											'app.model_management.reserved'
										)}: {reserved(worker) === null
											? t('app.model_management.unknown')
											: formatSize(reserved(worker) ?? 0)}</span
									>{/if}
								{#if previewSpace}<span class:text-destructive={!previewSpace.allowed}
										><span class="disk-swatch disk-preview" class:over={!previewSpace.allowed}
										></span>{t('app.model_management.after_install').replace(
											'{free}',
											known && previewSpace.freeAfter !== null
												? formatSize(previewSpace.freeAfter)
												: t('app.model_management.unknown')
										)}{!previewSpace.allowed
											? ` · ${t(reserved(worker) === null ? 'app.model_management.unknown_reservation' : 'app.model_management.margin')}`
											: ''}</span
									>{/if}
							</div>
						</div>
					{:else}<p>
							{t('app.model_management.disk_usage')}: {t('app.model_management.unknown')}
						</p>{/if}
				{/if}
				<div class="model-grid">
					{#each worker.models as model (model.id)}
						{@const operation = pending[key(worker.worker_id, model.id)]}
						{@const active =
							model.download?.state === 'queued' || model.download?.state === 'downloading'}
						<div
							class="flex min-w-0 flex-col gap-2 rounded-xl border bg-card p-4"
							data-model={model.id}
						>
							<div class="flex flex-wrap items-center gap-2">
								{#if !model.installed && !active && !pending[key(worker.worker_id, model.id)]}<input
										type="checkbox"
										aria-label={model.name}
										checked={(selected[worker.worker_id] ?? []).includes(model.id)}
										disabled={model.needs_token || busy}
										onchange={() => toggle(worker.worker_id, model.id)}
									/>{/if}
								<strong class="text-base font-semibold" data-heading tabindex="-1"
									>{model.name}</strong
								>
								{#if model.suggested}<Badge variant="secondary" class="suggested-badge"
										>{t('app.model_management.suggested')}</Badge
									>{/if}
							</div>
							<div class="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
								{#each [...model.capabilities.map( (capability) => capabilityLabel(capability) ), tierLabel(model.tier)] as chip}<span
										class="rounded-md border px-2 py-0.5">{chip}</span
									>{/each}
								<span class="text-dim self-center"
									>{model.license_id || t('app.model_management.unknown')}</span
								>
							</div>
							<div class="grid grid-cols-2 gap-3 text-sm">
								{#if model.download_bytes === null || model.download_bytes >= 5 * 1024 ** 2}
									<div>
										<span class="text-muted-foreground flex items-center gap-1"
											><HardDrive size={15} aria-hidden="true" />
											{t('app.model_management.download_size')}</span
										><strong
											>{knownSize(model.download_bytes) == null
												? t('app.model_management.unknown')
												: formatSize(model.download_bytes ?? 0)}</strong
										>
									</div>
								{/if}
								<div>
									<span class="text-muted-foreground flex items-center gap-1"
										><MemoryStick size={15} aria-hidden="true" />
										{t('app.model_management.vram')}</span
									><strong
										>{knownSize(model.min_vram_gb) === null
											? t('app.model_management.unknown')
											: formatSize(model.min_vram_gb * GIB)}</strong
									>
								</div>
							</div>
							<div class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
								{#if model.installed}<Badge variant="outline" class="status-success"
										><Check aria-hidden="true" />{t('app.model_management.installed')}</Badge
									>
								{:else if active}<Badge variant="outline" class="status-info"
										><Download aria-hidden="true" />{t(
											model.download?.state === 'queued'
												? 'app.model_management.queued'
												: 'app.model_management.downloading'
										)}</Badge
									>
								{:else if !operation && (model.needs_token || model.download?.error === 'needs_token')}<span
										id={`access-${encodeURIComponent(worker.worker_id)}-${encodeURIComponent(model.id)}`}
										>{t('app.model_management.access_badge')}</span
									>
								{:else if model.download?.state === 'failed' && model.download.error !== 'cancelled' && model.download.error !== 'not_running'}<Badge
										variant="outline"
										class="status-error"
										><AlertTriangle aria-hidden="true" />{t('app.model_management.failed')}</Badge
									>
								{:else if !operation}<Badge variant="outline" class="text-muted-foreground"
										>{t('app.model_management.not_installed')}</Badge
									>{/if}
								{#if !model.fits}<span
										>{t(
											knownSize(worker.hardware?.vram_total_bytes)
												? 'app.model_management.slow_gpu'
												: 'app.model_management.slow_gpu_unknown'
										)}</span
									>{/if}
							</div>
							{#if !model.fits || (!model.installed && (model.needs_token || model.download?.error === 'needs_token'))}
								<details class="text-muted-foreground text-xs">
									<summary
										class="w-fit cursor-pointer rounded-sm focus-visible:ring-2 focus-visible:ring-ring"
										>{t('app.model_management.details')}</summary
									>
									{#if !model.fits}<p class="mt-2">
											{t(
												!knownSize(worker.hardware?.vram_total_bytes)
													? 'app.model_management.needs_vram_unknown'
													: 'app.model_management.needs_vram'
											)
												.replace(
													'{vram}',
													knownSize(model.min_vram_gb) === null
														? t('app.model_management.unknown')
														: formatSize(model.min_vram_gb * GIB)
												)
												.replace(
													'{gpu_vram}',
													!knownSize(worker.hardware?.vram_total_bytes)
														? t('app.model_management.unknown')
														: formatSize(worker.hardware?.vram_total_bytes ?? 0)
												)}
										</p>
									{/if}
									{#if !model.installed && (model.needs_token || model.download?.error === 'needs_token')}
										<p class="mt-1">{t('app.model_management.access_help')}</p>
										<a
											class="underline"
											href="https://huggingface.co/docs/hub/security-tokens"
											target="_blank"
											rel="noreferrer">{t('app.model_management.docs')}</a
										>
									{/if}
								</details>
							{/if}
							{#if (operation && (operation.action === 'remove' || !active || !answers(model.download, operation))) || (active && (model.download?.kind === 'remove' || model.download?.kind === 'cancel'))}
								<p role="status">{t('app.model_management.waiting')}</p>
								{#if operation?.action === 'install' && !(active && (model.download?.kind === 'remove' || model.download?.kind === 'cancel'))}<Button
										variant="outline"
										disabled={busy}
										onclick={() => change(worker, model, 'cancel')}
										>{t('app.model_management.cancel')}</Button
									>{/if}
							{/if}
							{#if active && model.download?.kind === 'install' && (!operation || (operation.action !== 'remove' && answers(model.download, operation)))}<label
									>{model.download.bytes_total
										? new Intl.NumberFormat(getLocale(), {
												style: 'percent',
												maximumFractionDigits: 0
											}).format(Math.min(1, model.download.bytes_done / model.download.bytes_total))
										: t('app.model_management.unknown')} · {formatSize(model.download.bytes_done)} / {model
										.download.bytes_total
										? formatSize(model.download.bytes_total)
										: t('app.model_management.unknown')}<progress
										class="block w-full"
										aria-label={t('app.model_management.progress_label').replace(
											'{model}',
											model.name
										)}
										value={model.download.bytes_total ? model.download.bytes_done : undefined}
										max={model.download.bytes_total || 1}
									></progress></label
								><Button
									variant="outline"
									disabled={busy}
									onclick={() => change(worker, model, 'cancel')}
									>{t('app.model_management.cancel')}</Button
								>{/if}
							{#if (!operation || answers(model.download, operation)) && model.download?.state === 'failed' && model.download.error === 'not_running'}<p
									role="status"
								>
									{t('app.model_management.not_running')}
								</p>
							{:else if (!operation || answers(model.download, operation)) && model.download?.state === 'failed' && model.download.error === 'cancelled'}<p
									role="status"
								>
									{t('app.model_management.cancelled')}
								</p>
							{:else if (!operation || answers(model.download, operation)) && model.download?.state === 'failed' && model.download.error !== 'needs_token'}<p
									role="alert"
								>
									{t('app.model_management.failed')}: {model.download.error}
								</p>{/if}
							{#each ['install', 'cancel', 'remove'] as action}
								{@const message = actionErrors[actionKey(worker.worker_id, model.id, action)]}
								{#if message}<p role="alert">
										{t(
											action === 'install'
												? 'app.model_management.action_install'
												: action === 'cancel'
													? 'app.model_management.cancel'
													: 'app.model_management.remove'
										)}: {message}
										<button onclick={() => dismiss(worker, model, action)}
											>{t('app.model_management.dismiss')}</button
										>
									</p>{/if}
							{/each}
							{#if !model.installed && !active && !operation && !(model.download?.kind === 'remove' && model.download.state === 'failed' && model.download.error === 'remove_failed')}
								{@const free = worker.disk_free_bytes ?? worker.hardware?.disk_free_bytes}
								{@const allowed =
									free != null &&
									!model.needs_token &&
									selectionSpace(free, [model.download_bytes], reserved(worker)).allowed}
								{#if !allowed && !model.needs_token}<p
										id={`margin-${encodeURIComponent(worker.worker_id)}-${encodeURIComponent(model.id)}`}
										class="text-sm text-muted-foreground"
									>
										{t(
											reserved(worker) === null
												? 'app.model_management.unknown_reservation'
												: 'app.model_management.margin'
										)}
									</p>{/if}
								<Button
									variant="outline"
									class="mt-auto aria-disabled:opacity-50 aria-disabled:cursor-not-allowed"
									aria-describedby={!allowed
										? `${model.needs_token ? 'access' : 'margin'}-${encodeURIComponent(worker.worker_id)}-${encodeURIComponent(model.id)}`
										: undefined}
									aria-disabled={!allowed || busy}
									onclick={() => {
										if (allowed && !busy) void install(worker, model);
									}}
									onmouseenter={() => (preview = { worker: worker.worker_id, model: model.id })}
									onmouseleave={() => (preview = null)}
									onfocus={() => (preview = { worker: worker.worker_id, model: model.id })}
									onblur={() => (preview = null)}
									>{model.download?.state === 'failed' &&
									model.download.error !== 'cancelled' &&
									model.download.error !== 'not_running'
										? t('app.model_management.retry')
										: t('app.model_management.action_install')}</Button
								>
							{/if}
							{#if model.installed || (model.download?.kind === 'remove' && model.download.state === 'failed' && model.download.error === 'remove_failed')}<Button
									variant="outline"
									disabled={busy || !!pending[key(worker.worker_id, model.id)]}
									onclick={() => change(worker, model, 'remove')}
									>{t('app.model_management.remove')}</Button
								>{/if}
						</div>
					{/each}
				</div>
				{#if picks.length}
					<div class="selection-footer" data-selection-footer use:footerSpace>
						<div class="text-sm">
							<strong
								>{t('app.model_management.selected')}: {picks.some(
									(m) => knownSize(m.download_bytes) == null
								)
									? t('app.model_management.unknown')
									: formatSize(
											picks.reduce((total, model) => total + (model.download_bytes ?? 0), 0)
										)}</strong
							>
							<span class="text-muted-foreground">
								· {t('app.model_management.free_after')}: {space == null ||
								picks.some((m) => knownSize(m.download_bytes) == null)
									? t('app.model_management.unknown')
									: space.freeAfter === null
										? t('app.model_management.unknown')
										: formatSize(space.freeAfter)}</span
							>
							{#if !space?.allowed}<p role="status" class="text-destructive">
									{t(
										reserved(worker) === null
											? 'app.model_management.unknown_reservation'
											: 'app.model_management.margin'
									)}
								</p>{/if}
						</div>
						<Button disabled={busy || !space?.allowed} onclick={() => install(worker)}
							>{t('app.model_management.install')}</Button
						>
					</div>
				{/if}
			{/if}
		</section>
	{/each}
</div>

<style>
	.disk-track {
		display: flex;
		height: 12px;
		overflow: hidden;
		border-radius: 999px;
		background: var(--muted);
		border: 1px solid var(--muted-foreground);
	}
	.disk-used {
		background: var(--primary);
	}
	.disk-free {
		background: var(--muted);
		border: 1px solid var(--muted-foreground);
	}
	.disk-reserved {
		background: var(--status-running);
	}
	.disk-preview {
		background: repeating-linear-gradient(
			135deg,
			var(--muted-foreground) 0 3px,
			var(--muted) 3px 6px
		);
	}
	.disk-preview.over {
		background: var(--destructive);
	}
	.disk-swatch {
		display: inline-block;
		width: 10px;
		height: 10px;
		margin-right: 6px;
		border-radius: 2px;
	}
	.selection-footer {
		position: sticky;
		bottom: 0;
		z-index: 1;
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		align-items: center;
		gap: 12px;
		padding: 12px 16px;
		border: 1px solid var(--muted-foreground);
		border-radius: 6px;
		background: var(--card);
	}
	:global([data-slot='badge'].suggested-badge) {
		background: var(--accent);
		color: var(--accent-foreground);
		border-color: var(--muted-foreground);
	}
	:global([data-slot='badge'].status-success) {
		color: var(--status-success);
		border-color: currentColor;
	}
	:global([data-slot='badge'].status-info) {
		color: var(--status-running);
		border-color: currentColor;
	}
	:global([data-slot='badge'].status-error) {
		color: var(--destructive);
		border-color: currentColor;
	}

	.model-grid {
		display: grid;
		align-items: start;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, max(17rem, (100% - 3rem) / 4)), 1fr));
		gap: 1rem;
	}
</style>
