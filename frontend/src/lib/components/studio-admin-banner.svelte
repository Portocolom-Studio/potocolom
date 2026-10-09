<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import { adminBannerPutBody, type BannerConfig } from '$lib/status-banner-logic';
	import { adminErrorMessage, recheckAdminAccess } from '$lib/studio-admin-logic';
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import { Dialog } from 'bits-ui';

	type Kind = 'high_demand' | 'degraded' | 'maintenance';
	type MessageChoice = 'default' | 'custom';
	type PendingAction = { kind: 'set' } | { kind: 'clear' };

	let current = $state<BannerConfig>(null);
	let loading = $state(true);
	let loadError = $state('');
	let selectedKind = $state<Kind>('high_demand');
	let messageChoice = $state<MessageChoice>('default');
	let customText = $state('');
	let pendingAction = $state<PendingAction | null>(null);
	let confirmationOpen = $state(false);
	let actionError = $state('');
	let actionSubmitting = $state(false);

	async function apiError(response: Response): Promise<string> {
		if (response.status === 403) void recheckAdminAccess();
		const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
		return adminErrorMessage(body?.detail, response.statusText);
	}

	async function loadCurrent(): Promise<void> {
		loading = true;
		loadError = '';
		try {
			const response = await apiFetch('/api/v1/config');
			if (!response.ok) {
				loadError = await apiError(response);
				return;
			}
			const body = (await response.json()) as { banner?: BannerConfig };
			current = body.banner ?? null;
			if (current) {
				selectedKind = current.kind;
				messageChoice = current.custom_text ? 'custom' : 'default';
				customText = current.custom_text ?? '';
			}
		} catch {
			loadError = t('app.admin.request_failed');
		} finally {
			loading = false;
		}
	}

	onMount(loadCurrent);

	function requestSet(): void {
		pendingAction = { kind: 'set' };
		confirmationOpen = true;
	}

	function requestClear(): void {
		pendingAction = { kind: 'clear' };
		confirmationOpen = true;
	}

	function cancelConfirmation(): void {
		confirmationOpen = false;
		pendingAction = null;
		actionError = '';
	}

	async function confirmAction(): Promise<void> {
		const action = pendingAction;
		if (!action || actionSubmitting) return;
		actionSubmitting = true;
		actionError = '';
		try {
			const response =
				action.kind === 'set'
					? await apiFetch('/api/v1/admin/banner', {
							method: 'PUT',
							headers: { 'content-type': 'application/json' },
							body: JSON.stringify(adminBannerPutBody(selectedKind, messageChoice, customText))
						})
					: await apiFetch('/api/v1/admin/banner', { method: 'DELETE' });
			if (!response.ok) {
				actionError = await apiError(response);
				return;
			}
			confirmationOpen = false;
			pendingAction = null;
			await loadCurrent();
		} catch {
			actionError = t('app.admin.request_failed');
		} finally {
			actionSubmitting = false;
		}
	}

	const confirmationTitle = $derived(
		pendingAction?.kind === 'clear'
			? t('app.admin.banner_confirm_clear_title')
			: t('app.admin.banner_confirm_set_title')
	);
	const confirmationText = $derived(
		pendingAction?.kind === 'clear'
			? t('app.admin.banner_confirm_clear_text')
			: t('app.admin.banner_confirm_set_text')
	);
</script>

<div class="flex min-h-0 flex-col gap-4 overflow-auto pb-2">
	<Card.Root class="p-0 [--card-spacing:0]">
		<Card.Header class="border-border border-b px-4 py-3">
			<Card.Title class="text-base">{t('app.admin.banner')}</Card.Title>
			<Card.Description>{t('app.admin.banner_sub')}</Card.Description>
		</Card.Header>
		<Card.Content class="flex flex-col gap-4 p-4">
			{#if loadError}
				<p role="alert" class="text-destructive text-sm">{loadError}</p>
			{:else if loading}
				<p role="status" class="text-muted-foreground text-sm">
					{t('app.admin.loading')}
				</p>
			{:else}
				<p class="text-muted-foreground text-sm" data-testid="admin-banner-current">
					{t('app.admin.banner_current')}:
					{current
						? `${t(`app.admin.banner_kind_${current.kind}` as Parameters<typeof t>[0])}${current.custom_text ? ` - ${current.custom_text}` : ''}`
						: t('app.admin.banner_none')}
				</p>
			{/if}

			<label class="flex flex-col gap-1 text-xs font-medium">
				{t('app.admin.banner_kind')}
				<select
					data-testid="admin-banner-kind"
					class="border-input bg-background h-9 rounded-md border px-2 text-sm"
					bind:value={selectedKind}
				>
					<option value="high_demand">{t('app.admin.banner_kind_high_demand')}</option>
					<option value="degraded">{t('app.admin.banner_kind_degraded')}</option>
					<option value="maintenance">{t('app.admin.banner_kind_maintenance')}</option>
				</select>
			</label>

			<label class="flex flex-col gap-1 text-xs font-medium">
				{t('app.admin.banner_message')}
				<select
					data-testid="admin-banner-message-choice"
					class="border-input bg-background h-9 rounded-md border px-2 text-sm"
					bind:value={messageChoice}
				>
					<option value="default">{t('app.admin.banner_message_default')}</option>
					<option value="custom">{t('app.admin.banner_message_custom')}</option>
				</select>
			</label>

			{#if messageChoice === 'custom'}
				<label class="flex flex-col gap-1 text-xs font-medium">
					{t('app.admin.banner_custom_text')}
					<textarea
						data-testid="admin-banner-custom-text"
						class="border-input bg-background rounded-md border px-2 py-1.5 text-sm"
						maxlength="280"
						rows="2"
						bind:value={customText}></textarea>
					<span class="text-muted-foreground font-normal">
						{t('app.admin.banner_custom_text_hint')}
					</span>
				</label>
			{/if}

			{#if actionError}
				<p role="alert" class="text-destructive text-sm">{actionError}</p>
			{/if}

			<div class="flex gap-2">
				<Button data-testid="admin-banner-set" onclick={requestSet}>
					{t('app.admin.banner_set')}
				</Button>
				<Button
					data-testid="admin-banner-clear"
					variant="outline"
					disabled={current === null}
					onclick={requestClear}
				>
					{t('app.admin.banner_clear')}
				</Button>
			</div>
		</Card.Content>
	</Card.Root>
</div>

<Dialog.Root bind:open={confirmationOpen}>
	<Dialog.Portal>
		<Dialog.Overlay class="bg-background/80 fixed inset-0 z-50 backdrop-blur-sm" />
		<Dialog.Content
			class="bg-popover text-popover-foreground fixed top-1/2 left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border p-6 shadow-lg"
		>
			<Dialog.Title class="text-lg font-semibold">{confirmationTitle}</Dialog.Title>
			<Dialog.Description class="text-muted-foreground mt-2 text-sm">
				{confirmationText}
			</Dialog.Description>
			{#if actionError}
				<p role="alert" class="text-destructive mt-3 text-sm">{actionError}</p>
			{/if}
			<div class="mt-6 flex justify-end gap-2">
				<Button variant="outline" disabled={actionSubmitting} onclick={cancelConfirmation}>
					{t('app.admin.cancel')}
				</Button>
				<Button
					data-testid="admin-banner-confirm"
					disabled={actionSubmitting}
					onclick={confirmAction}
				>
					{actionSubmitting ? t('app.admin.saving') : t('app.admin.confirm')}
				</Button>
			</div>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
