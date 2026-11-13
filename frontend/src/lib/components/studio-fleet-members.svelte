<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import {
		adminApiError,
		adminErrorMessage,
		isRecentAuthenticationRequired,
		recheckAdminAccess
	} from '$lib/studio-admin-logic';
	import { formatDateTime, t } from '$lib/i18n.svelte';
	import {
		awaitingMachine,
		builtInWorkers,
		fleetAddress,
		fleetPollInterval,
		formatTimeLeft,
		isSourceBuildImage,
		listedMembers,
		memberState,
		rowActions,
		validLabel,
		workerCommand,
		type FleetDevice,
		type FleetMember
	} from '$lib/fleet-members';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
	import { Dialog } from 'bits-ui';
	import { studio } from '$lib/studio.svelte';

	type Created = {
		id: string;
		label: string;
		device: FleetDevice;
		join_token: string;
		expires_at: string;
		worker_image: string;
	};
	type Pending = {
		id: string;
		expiresAt: string;
		command: string;
		insecure: boolean;
		sourceBuild: boolean;
	};
	type Target = { id: string; label: string };

	// The page origin decides the address the worker dials. On localhost the
	// dialog asks for another address instead (see fleetAddress).
	const direct = fleetAddress(typeof window === 'undefined' ? '' : window.location.origin);

	let members = $state<FleetMember[]>([]);
	let builtIn = $state<string[]>([]);
	let loading = $state(true);
	let loadError = $state('');
	let now = $state(Date.now());
	let rowBusy = $state<Record<string, boolean>>({});
	let rowError = $state<Record<string, string>>({});

	let addOpen = $state(false);
	// Counts openings of the Add dialog, so a create answer that arrives after the
	// dialog was closed and opened again does not fill the new form.
	let addOpening = 0;
	let addButton = $state<HTMLElement | null>(null);
	let name = $state('');
	let device = $state<FleetDevice>('cuda');
	let lanOrigin = $state('');
	let formError = $state('');
	let formRecentAuthHint = $state(false);
	let submitting = $state(false);
	let pending = $state<Pending | null>(null);
	let copyHint = $state('');
	let commandField: HTMLTextAreaElement | null = null;

	let removeOpen = $state(false);
	let removeTarget = $state<Target | null>(null);
	let removeSubmitting = $state(false);

	let alive = false;
	let timer: ReturnType<typeof setTimeout> | undefined;
	let refreshVersion = 0;

	const address = $derived(direct.kind === 'loopback' ? fleetAddress(lanOrigin) : direct);
	const addressProblem = $derived.by(() => {
		if (address.kind === 'refused') return 'app.admin.fleet_address_public_http';
		if (address.kind === 'loopback') return 'app.admin.fleet_address_loopback';
		if (address.kind === 'invalid' && (direct.kind !== 'loopback' || lanOrigin.trim() !== ''))
			return 'app.admin.fleet_address_invalid';
		return null;
	});
	const canSubmit = $derived(
		validLabel(name) && (address.kind === 'secure' || address.kind === 'insecure') && !submitting
	);
	const rows = $derived(listedMembers(members));
	const pendingMember = $derived(
		pending === null ? undefined : members.find((member) => member.id === pending?.id)
	);
	const pendingStatus = $derived(
		pendingMember === undefined
			? { key: 'app.admin.fleet_state_waiting' as const, left: null, seen: null }
			: memberState(pendingMember, now)
	);
	const timeLeft = $derived(pending === null ? '' : formatTimeLeft(pending.expiresAt, now));

	function schedule(): void {
		clearTimeout(timer);
		const interval = fleetPollInterval(alive, awaitingMachine(members, pending?.id ?? null));
		if (interval !== null) timer = setTimeout(() => void refresh(), interval);
	}

	async function refresh(): Promise<void> {
		clearTimeout(timer);
		const version = ++refreshVersion;
		try {
			const [membersResponse, modelsResponse] = await Promise.all([
				apiFetch('/api/v1/admin/fleet/members'),
				apiFetch('/api/v1/admin/models')
			]);
			if (!alive || version !== refreshVersion) return;
			const failed = !membersResponse.ok
				? membersResponse
				: !modelsResponse.ok
					? modelsResponse
					: null;
			if (failed) {
				const message = await adminApiError(failed);
				if (alive && version === refreshVersion) loadError = message;
				return;
			}
			const listed = (await membersResponse.json()) as FleetMember[];
			const workers = (await modelsResponse.json()) as { worker_id: string }[];
			// Checked again: a newer refresh may have started while the bodies were read.
			if (!alive || version !== refreshVersion) return;
			members = listed;
			builtIn = builtInWorkers(workers);
			now = Date.now();
			loadError = '';
		} catch {
			if (alive && version === refreshVersion) loadError = t('app.admin.request_failed');
		} finally {
			if (alive && version === refreshVersion) {
				loading = false;
				schedule();
			}
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

	// The first-run GPU card sends the user here with the Add dialog already open.
	// The flag is cleared on use, so a later visit to this tab does not reopen it.
	$effect(() => {
		if (!studio.fleetAddRequested) return;
		studio.fleetAddRequested = false;
		addOpen = true;
	});

	function stateText(item: FleetMember): string {
		const label = memberState(item, now);
		let text = t(label.key);
		if (label.left !== null)
			text += ` (${t('app.admin.fleet_time_left').replace('{time}', label.left)})`;
		if (label.seen !== null)
			text += ` (${t('app.admin.fleet_last_seen').replace('{time}', formatDateTime(label.seen))})`;
		return text;
	}

	async function setPaused(item: FleetMember, paused: boolean): Promise<void> {
		rowBusy[item.id] = true;
		rowError[item.id] = '';
		try {
			const response = await apiFetch(
				`/api/v1/admin/fleet/members/${encodeURIComponent(item.id)}`,
				{
					method: 'PATCH',
					headers: { 'content-type': 'application/json' },
					body: JSON.stringify({ paused })
				}
			);
			if (!response.ok) {
				rowError[item.id] = await adminApiError(response);
				return;
			}
			await refresh();
		} catch {
			rowError[item.id] = t('app.admin.request_failed');
		} finally {
			rowBusy[item.id] = false;
		}
	}

	function requestRemove(item: FleetMember): void {
		removeTarget = { id: item.id, label: item.label };
		removeOpen = true;
	}

	function closeRemove(): void {
		removeOpen = false;
		removeTarget = null;
	}

	async function confirmRemove(): Promise<void> {
		const target = removeTarget;
		if (target === null || removeSubmitting) return;
		removeSubmitting = true;
		rowError[target.id] = '';
		try {
			const response = await apiFetch(
				`/api/v1/admin/fleet/members/${encodeURIComponent(target.id)}`,
				{
					method: 'DELETE'
				}
			);
			closeRemove();
			if (!response.ok) {
				rowError[target.id] = await adminApiError(response);
				return;
			}
			await refresh();
			// The row and its Remove button are gone, so focus goes to the next action.
			addButton?.focus();
		} catch {
			closeRemove();
			rowError[target.id] = t('app.admin.request_failed');
		} finally {
			removeSubmitting = false;
		}
	}

	function resetAdd(): void {
		name = '';
		lanOrigin = '';
		formError = '';
		formRecentAuthHint = false;
		copyHint = '';
		// Closing discards the join token: nothing else keeps a copy of it.
		pending = null;
		schedule();
	}

	function setAddOpen(open: boolean): void {
		addOpen = open;
		addOpening += 1;
		if (!open) resetAdd();
	}

	async function submitAdd(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		if (!canSubmit || (address.kind !== 'secure' && address.kind !== 'insecure')) return;
		const chosen = address;
		const insecure = chosen.kind === 'insecure';
		const opening = addOpening;
		submitting = true;
		formError = '';
		formRecentAuthHint = false;
		try {
			const response = await apiFetch('/api/v1/admin/fleet/members', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ label: name.trim(), device })
			});
			if (!response.ok) {
				const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
				if (response.status === 403 && isRecentAuthenticationRequired(body?.detail))
					formRecentAuthHint = true;
				formError = adminErrorMessage(body?.detail, response.statusText);
				if (response.status === 403) void recheckAdminAccess();
				return;
			}
			const created = (await response.json()) as Created;
			// The dialog may have been closed while the request was in flight. The
			// token then has no screen to show on, so it is dropped here.
			if (addOpen && opening === addOpening) {
				pending = {
					id: created.id,
					expiresAt: created.expires_at,
					insecure,
					sourceBuild: isSourceBuildImage(created.worker_image),
					command: workerCommand({
						device: created.device,
						memberId: created.id,
						wsUrl: chosen.wsUrl,
						token: created.join_token,
						image: created.worker_image,
						insecure
					})
				};
			}
			if (opening === addOpening) name = '';
			await refresh();
		} catch {
			formError = t('app.admin.request_failed');
		} finally {
			submitting = false;
		}
	}

	async function copyCommand(): Promise<void> {
		if (pending === null) return;
		try {
			await navigator.clipboard.writeText(pending.command);
			copyHint = t('app.admin.copy_link_copied');
		} catch {
			commandField?.select();
			copyHint = t('app.admin.fleet_copy_failed');
		}
	}
</script>

<Card.Root class="p-0 [--card-spacing:0]">
	<Card.Header
		class="border-border flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3"
	>
		<div>
			<Card.Title class="text-base">{t('app.admin.fleet_members_title')}</Card.Title>
			<Card.Description>{t('app.admin.fleet_members_sub')}</Card.Description>
		</div>
		<Button bind:ref={addButton} data-testid="admin-fleet-add" onclick={() => setAddOpen(true)}>
			{t('app.admin.fleet_add')}
		</Button>
	</Card.Header>
	<Card.Content class="p-0">
		{#if loadError}
			<div class="flex flex-wrap items-center justify-between gap-3 p-4">
				<p role="alert" class="text-destructive text-sm">{loadError}</p>
				<Button variant="outline" size="sm" onclick={() => void refresh()}>
					{t('app.admin.fleet_retry')}
				</Button>
			</div>
		{/if}
		{#if loading}
			<p role="status" class="text-muted-foreground p-4 text-sm">
				{t('app.admin.fleet_members_loading')}
			</p>
		{:else if rows.length + builtIn.length > 0}
			<ul class="divide-border divide-y" data-testid="admin-fleet-members">
				{#each builtIn as workerId (workerId)}
					<li
						data-testid="admin-fleet-builtin"
						class="flex flex-wrap items-center justify-between gap-3 px-4 py-3"
					>
						<span class="break-all text-sm font-medium">{workerId}</span>
						<Badge variant="secondary">{t('app.admin.fleet_builtin')}</Badge>
					</li>
				{/each}
				{#each rows as item (item.id)}
					{@const actions = rowActions(item)}
					<li data-testid="admin-fleet-member" class="flex flex-col gap-2 px-4 py-3">
						<div class="flex flex-wrap items-center justify-between gap-3">
							<div class="flex min-w-0 flex-col gap-1">
								<span class="break-all text-sm font-medium">{item.label}</span>
								<span class="text-muted-foreground flex flex-wrap gap-x-2 text-xs">
									<span>
										{item.device === 'cuda'
											? t('app.admin.fleet_device_nvidia')
											: t('app.admin.fleet_device_amd')}
									</span>
									<span>{stateText(item)}</span>
								</span>
							</div>
							<div class="flex flex-wrap items-center gap-2">
								{#if item.paused}
									<Badge variant="secondary" data-testid="admin-fleet-paused">
										{t('app.admin.fleet_paused')}
									</Badge>
								{/if}
								{#if actions.pause !== null}
									{@const pauseLabel =
										actions.pause === 'resume'
											? t('app.admin.fleet_resume')
											: t('app.admin.fleet_pause')}
									<Button
										variant="outline"
										size="sm"
										disabled={rowBusy[item.id]}
										data-testid="admin-fleet-pause"
										aria-label={`${pauseLabel}: ${item.label}`}
										onclick={() => void setPaused(item, actions.pause === 'pause')}
									>
										{pauseLabel}
									</Button>
								{/if}
								{#if actions.remove}
									<Button
										variant="outline"
										size="sm"
										disabled={rowBusy[item.id]}
										data-testid="admin-fleet-remove"
										aria-label={`${t('app.admin.fleet_remove')}: ${item.label}`}
										onclick={() => requestRemove(item)}
									>
										{t('app.admin.fleet_remove')}
									</Button>
								{/if}
							</div>
						</div>
						{#if rowError[item.id]}
							<p role="alert" class="text-destructive text-sm">{rowError[item.id]}</p>
						{/if}
					</li>
				{/each}
			</ul>
		{:else if !loadError}
			<p class="text-muted-foreground p-4 text-sm">{t('app.admin.fleet_members_empty')}</p>
		{/if}
	</Card.Content>
</Card.Root>

<Dialog.Root open={addOpen} onOpenChange={setAddOpen}>
	<Dialog.Portal>
		<Dialog.Overlay class="bg-background/80 fixed inset-0 z-50 backdrop-blur-sm" />
		<Dialog.Content
			class="bg-popover text-popover-foreground fixed top-1/2 left-1/2 z-50 max-h-[90vh] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border p-6 shadow-lg"
		>
			<Dialog.Title class="text-lg font-semibold">{t('app.admin.fleet_add_title')}</Dialog.Title>
			<Dialog.Description class="text-muted-foreground mt-2 text-sm">
				{t('app.admin.fleet_add_sub')}
			</Dialog.Description>

			{#if pending === null}
				<form class="mt-4 flex flex-col gap-4" onsubmit={submitAdd}>
					<label class="flex flex-col gap-1 text-xs font-medium">
						{t('app.admin.fleet_name_label')}
						<Input
							name="label"
							required
							maxlength={100}
							autocomplete="off"
							data-testid="admin-fleet-name"
							bind:value={name}
						/>
					</label>
					<label class="flex flex-col gap-1 text-xs font-medium">
						{t('app.admin.fleet_device_label')}
						<select
							data-testid="admin-fleet-device"
							class="border-input bg-background h-9 rounded-md border px-2 text-sm"
							bind:value={device}
						>
							<option value="cuda">{t('app.admin.fleet_device_nvidia')}</option>
							<option value="rocm">{t('app.admin.fleet_device_amd')}</option>
						</select>
					</label>

					{#if direct.kind === 'loopback'}
						<p class="text-muted-foreground text-sm">{t('app.admin.fleet_loopback_why')}</p>
						<label class="flex flex-col gap-1 text-xs font-medium">
							{t('app.admin.fleet_lan_label')}
							<Input
								name="lan-address"
								inputmode="url"
								autocomplete="off"
								spellcheck={false}
								placeholder="http://192.168.1.20:8080"
								data-testid="admin-fleet-lan"
								bind:value={lanOrigin}
							/>
						</label>
					{/if}

					{#if addressProblem}
						<p role="alert" class="text-destructive text-sm">{t(addressProblem)}</p>
					{:else if address.kind === 'insecure'}
						<p class="text-sm" data-testid="admin-fleet-insecure">
							{t('app.admin.fleet_insecure_notice')}
						</p>
					{/if}

					{#if formError}
						<div role="alert" class="text-destructive text-sm">
							<p>{formError}</p>
							{#if formRecentAuthHint}
								<p data-testid="admin-fleet-recent-auth-hint">
									{t('app.admin.fleet_recent_auth_hint')}
								</p>
							{/if}
						</div>
					{/if}

					<div class="flex justify-end gap-2">
						<Button type="button" variant="outline" onclick={() => setAddOpen(false)}>
							{t('app.admin.cancel')}
						</Button>
						<Button type="submit" data-testid="admin-fleet-create" disabled={!canSubmit}>
							{submitting ? t('app.admin.saving') : t('app.admin.fleet_create')}
						</Button>
					</div>
				</form>
			{:else}
				<div class="mt-4 flex flex-col gap-4">
					<div aria-live="polite" class="flex flex-col gap-1" data-testid="admin-fleet-status">
						<p class="text-sm font-medium">{t(pendingStatus.key)}</p>
						{#if pendingStatus.key === 'app.admin.fleet_state_waiting'}
							<p class="text-muted-foreground text-xs">
								{t('app.admin.fleet_time_left').replace('{time}', timeLeft)}
							</p>
						{/if}
					</div>

					<label class="flex flex-col gap-1 text-xs font-medium">
						{t('app.admin.fleet_command_label')}
						<textarea
							{@attach (node) => {
								commandField = node;
								return () => {
									commandField = null;
								};
							}}
							readonly
							rows={6}
							spellcheck={false}
							data-testid="admin-fleet-command"
							class="border-input bg-background w-full rounded-md border p-2 font-mono text-xs break-all"
							value={pending.command}></textarea>
					</label>

					<div class="flex flex-wrap items-center gap-2">
						<Button variant="outline" data-testid="admin-fleet-copy" onclick={copyCommand}>
							{t('app.admin.fleet_copy_command')}
						</Button>
						<p class="text-muted-foreground text-xs" aria-live="polite">{copyHint}</p>
					</div>

					<p class="text-muted-foreground text-xs">{t('app.admin.fleet_replace_note')}</p>
					{#if pending.insecure}
						<p class="text-sm">{t('app.admin.fleet_insecure_notice')}</p>
					{/if}
					{#if pending.sourceBuild}
						<p class="text-sm" data-testid="admin-fleet-source-build">
							{t('app.admin.fleet_source_build_notice')}
						</p>
					{/if}
					<p class="text-muted-foreground text-xs">{t('app.admin.fleet_token_note')}</p>

					<div class="flex justify-end">
						<Button
							variant="outline"
							data-testid="admin-fleet-close"
							onclick={() => setAddOpen(false)}
						>
							{t('app.admin.fleet_close')}
						</Button>
					</div>
				</div>
			{/if}
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>

<Dialog.Root bind:open={removeOpen}>
	<Dialog.Portal>
		<Dialog.Overlay class="bg-background/80 fixed inset-0 z-50 backdrop-blur-sm" />
		<Dialog.Content
			class="bg-popover text-popover-foreground fixed top-1/2 left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border p-6 shadow-lg"
		>
			<Dialog.Title class="text-lg font-semibold">{t('app.admin.fleet_remove_title')}</Dialog.Title>
			<Dialog.Description class="text-muted-foreground mt-2 text-sm">
				{t('app.admin.fleet_remove_text').replace('{label}', () => removeTarget?.label ?? '')}
			</Dialog.Description>
			<div class="mt-6 flex justify-end gap-2">
				<Button variant="outline" disabled={removeSubmitting} onclick={closeRemove}>
					{t('app.admin.cancel')}
				</Button>
				<Button
					data-testid="admin-fleet-remove-confirm"
					disabled={removeSubmitting}
					onclick={confirmRemove}
				>
					{removeSubmitting ? t('app.admin.saving') : t('app.admin.fleet_remove_confirm')}
				</Button>
			</div>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
