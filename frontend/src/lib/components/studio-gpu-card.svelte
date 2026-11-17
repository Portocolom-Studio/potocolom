<script lang="ts">
	import { tick, untrack } from 'svelte';
	import { apiFetch } from '$lib/api';
	import { account } from '$lib/account.svelte';
	import { accountRoleLabelKey } from '$lib/account-display';
	import FirstRunFrame from '$lib/components/first-run-frame.svelte';
	import { getLocale, t } from '$lib/i18n.svelte';
	import {
		DEVICE_LABELS,
		defaultGpuOption,
		failureKey,
		firstModels,
		gpuCardOpensFleet,
		hasCapability,
		markGpuCardDone,
		readyCopy,
		reservedBytes,
		rowStatus,
		setupWorker,
		shouldShowGpuCard,
		skipsModelStep,
		type AdminWorker,
		type CardKey,
		type FirstOption,
		type GpuOption,
		type UseClass
	} from '$lib/gpu-setup-card';
	import {
		formatSize,
		knownSize,
		pollInterval,
		selectionSpace,
		shouldPoll,
		sumKnown
	} from '$lib/model-management';
	import { Button } from '$lib/components/ui/button';
	import { markTourDone, shouldAutoStart } from '$lib/studio-tour';
	import { loadModels, openAdmin, openService, studio } from '$lib/studio.svelte';
	import { Dialog } from 'bits-ui';

	type Step = 2 | 3 | 4;

	// Decided once, at mount, before the models answer: the tour waits on
	// studio.gpuCardOpen, so the flag must already say so by then.
	studio.gpuCardOpen = shouldShowGpuCard(
		account.localModeConfirmed || account.current?.role === 'admin',
		() => localStorage
	);

	// Access can be lost while the card is open (the role is read again on window
	// focus). The card closes without its flag, so it comes back if access does.
	$effect(() => {
		if (studio.gpuCardOpen && !(account.localModeConfirmed || account.current?.role === 'admin'))
			studio.gpuCardOpen = false;
	});

	let step = $state<Step>(2);
	// Null while the list loads and when it failed. Both keep the "this" option.
	let workers = $state<AdminWorker[] | null>(null);
	let loading = $state(true);
	// The user's own pick wins over the default, which may still change while loading.
	let choice = $state<GpuOption | null>(null);
	const option = $derived(
		choice ?? defaultGpuOption(workers?.map((worker) => worker.worker_id) ?? null)
	);
	// Model ids the user unticked on step 3. Every other option is ticked.
	let unchecked = $state<string[]>([]);
	// Model ids that step 3 started, shown on step 4.
	let chosen = $state<string[]>([]);
	let showTour = $state(true);
	// The switch is offered only when the tour would start after the card: on the
	// first open of this page load, with the tour not yet done.
	let firstOpen = studio.gpuCardOpen;
	let tourOffered = $state(false);
	// Read when the card closes, so focus can return to the user menu.
	let openedFromMenu = false;
	// Bumped on every open. An install loop from an earlier open stops at its next await.
	let opening = 0;
	let installing = $state(false);
	let installAlert = $state<string | null>(null);
	// Counts finished list requests, so the poll below is scheduled again after each one.
	let polls = $state(0);
	let titleRef = $state<HTMLElement | null>(null);
	// Each list request gets a number; an answer to an older request is dropped.
	let version = 0;

	const list = $derived(workers ?? []);
	// Pinned when the user leaves step 2, so a worker that reports later cannot take
	// over the steps that follow.
	let setupId = $state<string | null>(null);
	const setup = $derived(
		setupId === null
			? setupWorker(list)
			: (list.find((worker) => worker.worker_id === setupId) ?? null)
	);
	// Once step 2 pinned a worker, the summary names only that worker: a missing one
	// shows as gone, never as another GPU.
	const gpuWorker = $derived(setupId === null ? (setup ?? list.at(0) ?? null) : setup);
	const offered = $derived(setup ? firstModels(setup) : []);
	const ready = $derived(readyCopy(workers));
	const hasRealtime = $derived(hasCapability(list, 'realtime'));

	function pending(item: FirstOption): boolean {
		return item.download !== null && shouldPoll([item.download.state]);
	}
	function ticked(item: FirstOption): boolean {
		return item.installed || pending(item) || !unchecked.includes(item.id);
	}
	function toggle(id: string): void {
		unchecked = unchecked.includes(id)
			? unchecked.filter((item) => item !== id)
			: [...unchecked, id];
	}

	// What step 3 starts: ticked, not installed, and not already downloading.
	const toInstall = $derived(
		offered.filter((item) => !item.installed && !pending(item) && ticked(item))
	);
	const sizesKnown = $derived(toInstall.every((item) => item.size !== null));
	const freeBytes = $derived(
		setup ? (setup.disk_free_bytes ?? setup.hardware?.disk_free_bytes ?? null) : null
	);
	const reserved = $derived(setup ? reservedBytes(setup) : 0);
	const total = $derived(sumKnown(toInstall.map((item) => item.size)));
	const space = $derived(
		freeBytes === null || !sizesKnown
			? null
			: selectionSpace(
					freeBytes,
					toInstall.map((item) => item.size),
					reserved
				)
	);
	const canDownload = $derived(
		!installing && (toInstall.length === 0 || (sizesKnown && space?.allowed === true))
	);
	// The reason the free-space rule refuses the selection, when it does.
	const spaceMessage = $derived<CardKey | null>(
		toInstall.length === 0 || !sizesKnown || space === null || space.allowed
			? null
			: reserved === null
				? 'app.model_management.unknown_reservation'
				: 'app.model_management.margin'
	);

	// The rows of step 4: the models step 3 started or that are installed already.
	const rows = $derived(
		offered.filter((item) => item.installed || pending(item) || chosen.includes(item.id))
	);
	const rowStates = $derived(rows.map((row) => row.download?.state ?? ''));

	// Milliseconds until the card asks the list again, or null when nothing on this
	// step waits on the worker: hardware not reported yet (step 2), sizes not known
	// yet (step 3), a download running (step 4).
	const delay = $derived.by(() => {
		if (workers === null || !studio.gpuCardOpen) return null;
		if (step === 2) return list.some((worker) => worker.hardware === null) ? 2000 : null;
		if (step === 3) {
			// Sizes still unknown, or another install still running: either can change
			// whether the free-space rule allows the download.
			const running = setup?.models.some(
				(model) => model.download !== null && shouldPoll([model.download.state])
			);
			return sizesKnown && reserved !== null && freeBytes !== null && !running ? null : 2000;
		}
		return pollInterval(true, shouldPoll(rowStates));
	});

	$effect(() => {
		const wait = delay;
		void polls;
		if (wait === null) return;
		const timer = setTimeout(() => void refresh(), wait);
		return () => clearTimeout(timer);
	});

	// Opening the card, from the first visit or from the user menu, starts at step 2
	// with a fresh list. The list is read again on every open.
	$effect(() => {
		if (!studio.gpuCardOpen) return;
		untrack(() => {
			version++;
			step = 2;
			choice = null;
			unchecked = [];
			chosen = [];
			showTour = true;
			tourOffered = firstOpen && shouldAutoStart(() => localStorage);
			firstOpen = false;
			opening++;
			openedFromMenu = studio.gpuCardFromMenu;
			studio.gpuCardFromMenu = false;
			setupId = null;
			installAlert = null;
			installing = false;
			workers = null;
			loading = true;
			void refresh();
		});
	});

	function installedCount(list: AdminWorker[] | null): number {
		return (list ?? []).reduce(
			(sum, worker) => sum + worker.models.filter((model) => model.installed).length,
			0
		);
	}

	async function refresh(): Promise<void> {
		const current = ++version;
		try {
			const response = await apiFetch('/api/v1/admin/models', {
				signal: AbortSignal.timeout(10_000)
			});
			if (response.ok) {
				const body = (await response.json()) as AdminWorker[];
				// Checked after the body: a reopen while it was read starts a newer request.
				if (current === version) {
					// A model that became installed must reach the studio's pickers too,
					// which read their own list.
					if (installedCount(workers) < installedCount(body)) void loadModels();
					workers = body;
				}
			}
		} catch {
			// A failed or slow list keeps what the card shows. A first load that failed
			// shows the "could not be read" note.
		} finally {
			if (current === version) {
				loading = false;
				polls++;
			}
		}
	}

	async function goTo(next: Step): Promise<void> {
		step = next;
		await tick();
		titleRef?.focus();
	}

	function finish(ending: GpuOption | 'dismissed'): void {
		if (!studio.gpuCardOpen) return;
		markGpuCardDone(() => localStorage);
		if (tourOffered && !showTour) markTourDone(() => localStorage);
		if (gpuCardOpensFleet(ending)) {
			studio.fleetAddRequested = true;
			openAdmin('fleet');
		}
		studio.gpuCardOpen = false;
	}

	function continueFromGpu(): void {
		if (option === 'other') {
			finish('other');
			return;
		}
		setupId = setup?.worker_id ?? null;
		void goTo(skipsModelStep(setup) ? 4 : 3);
	}

	// Returns null when the worker accepted the install, else the reason to show.
	async function startInstall(
		worker: string,
		model: string
	): Promise<'needs_token' | 'failed' | null> {
		try {
			const response = await apiFetch(
				`/api/v1/admin/workers/${encodeURIComponent(worker)}/models/${encodeURIComponent(model)}/install`,
				{ method: 'POST' }
			);
			if (response.ok) return null;
			const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
			return response.status === 409 && body?.detail === 'needs_token' ? 'needs_token' : 'failed';
		} catch {
			return 'failed';
		}
	}

	async function downloadChosen(): Promise<void> {
		const picks = toInstall;
		if (picks.length === 0) {
			void goTo(4);
			return;
		}
		if (!canDownload || setup === null) return;
		const worker = setup.worker_id;
		const open = opening;
		installing = true;
		installAlert = null;
		try {
			for (const item of picks) {
				const reason = await startInstall(worker, item.id);
				if (open !== opening) return;
				if (reason !== null) {
					installAlert = t('app.gpu_card.install_alert')
						.replace('{model}', item.name)
						.replace('{reason}', t(failureKey(reason)));
					return;
				}
			}
			chosen = picks.map((item) => item.id);
			void goTo(4);
		} finally {
			if (open === opening) {
				installing = false;
				void refresh();
			}
		}
	}

	function startDrawing(): void {
		openService('realtime_canvas');
		finish('dismissed');
	}

	function submit(event: SubmitEvent): void {
		event.preventDefault();
		if (step === 2) continueFromGpu();
		else if (step === 3) void downloadChosen();
		else if (hasRealtime) startDrawing();
		else finish('dismissed');
	}

	const size = (bytes: number) => formatSize(bytes, getLocale());

	function memoryText(bytes: number | null): string {
		const known = knownSize(bytes);
		return known === null
			? t('app.gpu_card.memory_unknown')
			: t('app.gpu_card.memory').replace('{size}', size(known));
	}

	const gpuValue = $derived.by(() => {
		if (gpuWorker === null)
			return setupId === null ? t('app.gpu_card.no_gpu') : t('app.gpu_card.gpu_gone');
		if (gpuWorker.hardware === null) return t('app.gpu_card.reading_gpu');
		return `${gpuWorker.hardware.gpu_name}, ${memoryText(gpuWorker.hardware.vram_total_bytes)}`;
	});

	const accountValue = $derived(
		account.localModeConfirmed || account.current === null
			? t('app.gpu_card.account_local')
			: `${account.current.email}, ${t(accountRoleLabelKey(account.current.role))}`
	);

	const headingText = $derived(
		step === 2
			? t('app.gpu_card.title')
			: step === 3
				? t('app.gpu_card.models_title')
				: t(ready.title)
	);
	const descriptionText = $derived(
		step === 2 ? t('app.gpu_card.body') : step === 3 ? t('app.gpu_card.models_body') : t(ready.body)
	);

	function useLabel(use: UseClass): CardKey {
		switch (use) {
			case 'text_to_image':
				return 'app.gpu_card.use_text_to_image';
			case 'realtime':
				return 'app.gpu_card.use_realtime';
			case 'upscale':
				return 'app.gpu_card.use_upscale';
		}
	}
	function useHint(use: UseClass): CardKey {
		switch (use) {
			case 'text_to_image':
				return 'app.gpu_card.use_text_to_image_hint';
			case 'realtime':
				return 'app.gpu_card.use_realtime_hint';
			case 'upscale':
				return 'app.gpu_card.use_upscale_hint';
		}
	}
</script>

{#snippet heading()}
	<Dialog.Title bind:ref={titleRef} tabindex={-1} class="text-2xl font-semibold outline-none">
		{headingText}
	</Dialog.Title>
{/snippet}

{#snippet content()}
	{#if step === 2}
		<fieldset class="flex flex-col gap-3">
			<legend class="mb-2 text-sm font-medium">{t('app.gpu_card.group_label')}</legend>
			<div class="grid gap-3 sm:grid-cols-2">
				<label
					class="flex cursor-pointer flex-col gap-3 rounded-xl border p-4 {option === 'this'
						? 'border-foreground'
						: 'border-border'}"
				>
					<span class="flex items-start justify-between gap-3">
						<span class="text-sm font-medium">{t('app.gpu_card.this_label')}</span>
						<input
							type="radio"
							name="gpu-location"
							value="this"
							class="mt-0.5"
							data-testid="gpu-card-this"
							checked={option === 'this'}
							onchange={() => (choice = 'this')}
						/>
					</span>
					<span class="text-sm" data-testid="gpu-card-workers">
						{#if loading}
							<span role="status" class="text-muted-foreground block"
								>{t('app.gpu_card.loading')}</span
							>
						{:else if workers === null}
							<span role="alert" class="text-destructive block">{t('app.gpu_card.unreadable')}</span
							>
						{:else if list.length === 0}
							<span class="text-muted-foreground block">{t('app.gpu_card.none_connected')}</span>
						{:else}
							<ul class="flex flex-col gap-2">
								{#each list as worker (worker.worker_id)}
									<li class="flex flex-col gap-0.5" data-testid="gpu-card-worker">
										<span class="flex items-center gap-2 font-medium">
											<span
												class="bg-status-success size-2 shrink-0 rounded-full"
												aria-hidden="true"
											></span>
											<span class="sr-only">{t('app.gpu_card.online')}</span>
											<span class="break-all">
												{worker.hardware === null
													? t('app.gpu_card.reading_gpu')
													: worker.hardware.gpu_name}
											</span>
										</span>
										<span class="text-muted-foreground pl-4 text-xs">
											{memoryText(worker.hardware?.vram_total_bytes ?? null)}{worker.device
												? `, ${DEVICE_LABELS[worker.device] ?? worker.device}`
												: ''}
										</span>
									</li>
								{/each}
							</ul>
						{/if}
					</span>
				</label>
				<label
					class="flex cursor-pointer flex-col gap-3 rounded-xl border p-4 {option === 'other'
						? 'border-foreground'
						: 'border-border'}"
				>
					<span class="flex items-start justify-between gap-3">
						<span class="text-sm font-medium">{t('app.gpu_card.other_label')}</span>
						<input
							type="radio"
							name="gpu-location"
							value="other"
							class="mt-0.5"
							data-testid="gpu-card-other"
							checked={option === 'other'}
							onchange={() => (choice = 'other')}
						/>
					</span>
					<span class="text-muted-foreground text-xs">{t('app.gpu_card.other_hint')}</span>
				</label>
			</div>
		</fieldset>
	{:else if step === 3}
		<fieldset class="flex flex-col gap-3">
			<legend class="sr-only">{t('app.gpu_card.models_title')}</legend>
			<div class="grid gap-3 sm:grid-cols-3">
				{#each offered as item (item.id)}
					{@const locked = item.installed || pending(item)}
					<label
						class="flex flex-col gap-3 rounded-xl border p-4 {ticked(item)
							? 'border-foreground'
							: 'border-border'} {locked ? '' : 'cursor-pointer'}"
					>
						<span class="flex items-start justify-between gap-3">
							<span class="text-sm font-medium">{t(useLabel(item.use))}</span>
							<input
								type="checkbox"
								class="mt-0.5"
								data-testid="gpu-card-model"
								data-model={item.id}
								checked={ticked(item)}
								disabled={locked || installing}
								onchange={() => toggle(item.id)}
							/>
						</span>
						<span class="text-muted-foreground text-xs">{t(useHint(item.use))}</span>
						<span class="mt-auto flex items-baseline justify-between gap-2 text-xs">
							<span class="font-medium">{item.name}</span>
							<span class="text-muted-foreground">
								{#if item.installed}
									{t('app.model_management.installed')}
								{:else if pending(item)}
									{t('app.model_management.downloading')}
								{:else if item.size === null}
									{t('app.gpu_card.size_unknown')}
								{:else}
									{size(item.size)}
								{/if}
							</span>
						</span>
					</label>
				{/each}
			</div>
		</fieldset>
		<div
			class="bg-card mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border p-4 text-sm"
		>
			<strong>
				{sizesKnown && total !== null
					? t('app.gpu_card.to_download').replace('{total}', size(total))
					: t('app.gpu_card.reading_sizes')}
			</strong>
			<span class="text-muted-foreground">
				{freeBytes === null
					? t('app.model_management.unknown')
					: t('app.gpu_card.free_disk').replace('{free}', size(freeBytes))}
			</span>
			{#if spaceMessage}
				<span role="status" class="text-destructive w-full">{t(spaceMessage)}</span>
			{/if}
		</div>
		{#if installAlert}
			<p role="alert" class="text-destructive text-sm">{installAlert}</p>
		{/if}
	{:else}
		<div class="flex flex-col gap-4">
			<ul class="flex flex-col gap-2">
				<li class="bg-card flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4">
					<span class="text-sm font-medium">{t('app.gpu_card.row_account')}</span>
					<span class="text-muted-foreground text-sm break-all">{accountValue}</span>
				</li>
				<li class="bg-card flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4">
					<span class="text-sm font-medium">{t('app.gpu_card.row_gpu')}</span>
					<span class="text-muted-foreground text-sm break-all">{gpuValue}</span>
				</li>
				{#each rows as row (row.id)}
					{@const status = rowStatus(row)}
					<li
						class="bg-card flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4"
						data-testid="gpu-card-row"
						data-model={row.id}
					>
						<span class="flex min-w-0 flex-col">
							<span class="text-sm font-medium">{row.name}</span>
							<span class="text-muted-foreground text-xs">{t(useLabel(row.use))}</span>
						</span>
						{#if status.kind === 'ready'}
							<span
								class="border-status-success text-status-success rounded-full border px-2 py-0.5 text-xs"
							>
								{t('app.gpu_card.ready')}
							</span>
						{:else if status.kind === 'progress'}
							<span class="flex items-center gap-3 text-xs">
								<span
									role="progressbar"
									aria-label={t('app.model_management.progress_label').replace('{model}', row.name)}
									aria-valuemin="0"
									aria-valuemax={status.total}
									aria-valuenow={status.done}
									class="bg-muted h-1.5 w-28 overflow-hidden rounded-full"
								>
									<span
										class="bg-foreground block h-full"
										style:width="{Math.round((100 * status.done) / status.total)}%"
									></span>
								</span>
								<span class="text-muted-foreground">
									{t('app.gpu_card.progress')
										.replace('{done}', size(status.done))
										.replace('{total}', size(status.total))}
								</span>
							</span>
						{:else if status.kind === 'failed'}
							<span class="text-destructive text-xs">{t(status.key)}</span>
						{:else}
							<span class="text-muted-foreground text-xs">{t('app.model_management.waiting')}</span>
						{/if}
					</li>
				{/each}
			</ul>
			{#if tourOffered}
				<label class="flex items-start gap-3 text-sm">
					<input
						type="checkbox"
						role="switch"
						class="mt-0.5"
						data-testid="gpu-card-tour"
						bind:checked={showTour}
					/>
					<span>
						<strong class="font-medium">{t('app.gpu_card.tour_label')}</strong>
						{t('app.gpu_card.tour_hint')}
					</span>
				</label>
			{/if}
		</div>
	{/if}
{/snippet}

{#snippet footer()}
	<Button
		variant="ghost"
		type="button"
		data-testid="gpu-card-later"
		onclick={() => finish('dismissed')}>{t('app.gpu_card.later')}</Button
	>
	<div class="ml-auto flex flex-wrap items-center gap-2">
		{#if step === 2}
			<Button type="submit" data-testid="gpu-card-continue" disabled={loading}
				>{t('app.gpu_card.continue')}</Button
			>
		{:else if step === 3}
			<Button
				variant="secondary"
				type="button"
				data-testid="gpu-card-back"
				disabled={installing}
				onclick={() => void goTo(2)}>{t('app.gpu_card.back')}</Button
			>
			<Button type="submit" data-testid="gpu-card-download" disabled={!canDownload}>
				{toInstall.length === 0 ? t('app.gpu_card.continue') : t('app.gpu_card.download_continue')}
			</Button>
		{:else}
			{#if hasRealtime}
				<Button
					variant="secondary"
					type="button"
					data-testid="gpu-card-open"
					onclick={() => finish('dismissed')}>{t('app.gpu_card.open_studio')}</Button
				>
				<Button type="submit" data-testid="gpu-card-start">{t('app.gpu_card.start_drawing')}</Button
				>
			{:else}
				<Button variant="secondary" type="submit" data-testid="gpu-card-open"
					>{t('app.gpu_card.open_studio')}</Button
				>
			{/if}
		{/if}
	</div>
{/snippet}

<Dialog.Root
	open={studio.gpuCardOpen}
	onOpenChange={(open) => {
		// Escape reaches the tour's window handler in the same keystroke that closes
		// the card. Finishing in a later task keeps that keystroke from also skipping
		// the tour that the card's end starts.
		if (!open) setTimeout(() => finish('dismissed'));
	}}
>
	<Dialog.Portal>
		<Dialog.Overlay class="bg-background/80 fixed inset-0 z-50" />
		<Dialog.Content
			data-testid="gpu-card"
			onOpenAutoFocus={(event) => {
				// The question, not the language switch that comes first in the frame.
				event.preventDefault();
				titleRef?.focus();
			}}
			onCloseAutoFocus={(event) => {
				// Opened from the user menu: focus goes back to that menu's trigger, which
				// the menu itself left when the card took focus.
				if (!openedFromMenu) return;
				event.preventDefault();
				document.querySelector<HTMLElement>('[data-user-menu-trigger]')?.focus();
			}}
			class="bg-popover text-popover-foreground fixed top-1/2 left-1/2 z-50 max-h-[90vh] w-[calc(100%-2rem)] max-w-3xl -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-xl border p-6 shadow-lg"
		>
			<form onsubmit={submit} data-testid="gpu-card-form">
				<FirstRunFrame {step} description={descriptionText} {heading} children={content} {footer} />
			</form>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
