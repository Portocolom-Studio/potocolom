<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import {
		adminErrorMessage,
		invitationExpired,
		inviteRequestBody,
		isRecentAuthenticationRequired,
		recheckAdminAccess
	} from '$lib/studio-admin-logic';
	import { formatDateTime, t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
	import { Dialog } from 'bits-ui';
	import type { Role } from '$lib/account-display';

	type OpenInvitation = {
		id: string;
		email: string;
		role: Role;
		expires_at: string;
		created_at: string;
	};

	type Minted = { id: string; email: string; role: Role; link: string; expires_at: string };

	type PendingAction =
		| { kind: 'invite' }
		| { kind: 'reveal'; id: string; email: string }
		| { kind: 'revoke'; id: string; email: string };

	let email = $state('');
	let role = $state<Role>('user');
	let formError = $state('');
	let formRecentAuthHint = $state(false);
	let submitting = $state(false);
	let minted = $state<Minted | null>(null);
	let linkCopyHint = $state('');
	let linkField = $state<HTMLInputElement | null>(null);

	let invitations = $state<OpenInvitation[]>([]);
	let listLoading = $state(true);
	let listError = $state('');

	let pendingAction = $state<PendingAction | null>(null);
	let confirmationOpen = $state(false);
	let confirmationError = $state('');
	let confirmationSubmitting = $state(false);

	function roleLabelKey(
		value: Role
	): 'app.admin.role_admin' | 'app.admin.role_viewer' | 'app.admin.role_user' {
		if (value === 'admin') return 'app.admin.role_admin';
		if (value === 'viewer') return 'app.admin.role_viewer';
		return 'app.admin.role_user';
	}

	async function apiError(response: Response): Promise<string> {
		if (response.status === 403) void recheckAdminAccess();
		const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
		return adminErrorMessage(body?.detail, response.statusText);
	}

	async function loadInvitations(): Promise<void> {
		listLoading = true;
		listError = '';
		try {
			const response = await apiFetch('/api/v1/invitations');
			if (!response.ok) {
				listError = await apiError(response);
				return;
			}
			invitations = (await response.json()) as OpenInvitation[];
		} catch {
			listError = t('app.admin.request_failed');
		} finally {
			listLoading = false;
		}
	}

	onMount(loadInvitations);

	function clearMinted(): void {
		minted = null;
		linkCopyHint = '';
	}

	function submitInvite(event: SubmitEvent): void {
		event.preventDefault();
		requestInvite();
	}

	function requestInvite(): void {
		if (email.trim() === '') return;
		formError = '';
		formRecentAuthHint = false;
		if (role === 'admin') {
			pendingAction = { kind: 'invite' };
			confirmationError = '';
			confirmationOpen = true;
			return;
		}
		void sendInvite();
	}

	function requestReveal(invitation: OpenInvitation): void {
		pendingAction = { kind: 'reveal', id: invitation.id, email: invitation.email };
		confirmationError = '';
		confirmationOpen = true;
	}

	function requestRevoke(invitation: OpenInvitation): void {
		pendingAction = { kind: 'revoke', id: invitation.id, email: invitation.email };
		confirmationError = '';
		confirmationOpen = true;
	}

	function cancelConfirmation(): void {
		confirmationOpen = false;
		pendingAction = null;
		confirmationError = '';
	}

	async function sendInvite(): Promise<void> {
		if (submitting) return;
		submitting = true;
		formError = '';
		formRecentAuthHint = false;
		try {
			const response = await apiFetch('/api/v1/invitations', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(inviteRequestBody(email, role))
			});
			if (!response.ok) {
				const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
				if (response.status === 403 && isRecentAuthenticationRequired(body?.detail)) {
					formRecentAuthHint = true;
				}
				formError = adminErrorMessage(body?.detail, response.statusText);
				if (response.status === 403) void recheckAdminAccess();
				return;
			}
			clearMinted();
			minted = (await response.json()) as Minted;
			email = '';
			await loadInvitations();
		} catch {
			formError = t('app.admin.request_failed');
		} finally {
			submitting = false;
		}
	}

	async function confirmAction(): Promise<void> {
		const action = pendingAction;
		if (!action || confirmationSubmitting) return;
		if (action.kind === 'invite') {
			confirmationOpen = false;
			pendingAction = null;
			await sendInvite();
			return;
		}
		confirmationSubmitting = true;
		confirmationError = '';
		try {
			const response =
				action.kind === 'reveal'
					? await apiFetch(`/api/v1/invitations/${encodeURIComponent(action.id)}/reveal`, {
							method: 'POST'
						})
					: await apiFetch(`/api/v1/invitations/${encodeURIComponent(action.id)}`, {
							method: 'DELETE'
						});
			if (!response.ok) {
				// Another administrator revoked it, or it was accepted: the row is
				// stale, and retrying would only 404 again.
				if (response.status === 404) {
					cancelConfirmation();
					await loadInvitations();
					return;
				}
				confirmationError = await apiError(response);
				return;
			}
			confirmationOpen = false;
			pendingAction = null;
			if (action.kind === 'revoke' && minted?.id === action.id) clearMinted();
			if (action.kind === 'reveal') {
				clearMinted();
				minted = (await response.json()) as Minted;
			}
			await loadInvitations();
		} catch {
			confirmationError = t('app.admin.request_failed');
		} finally {
			confirmationSubmitting = false;
		}
	}

	async function copyLink(): Promise<void> {
		if (!minted) return;
		try {
			await navigator.clipboard.writeText(minted.link);
			linkCopyHint = t('app.admin.copy_link_copied');
		} catch {
			linkField?.select();
			linkCopyHint = t('app.admin.copy_link_failed');
		}
	}

	const confirmationTitle = $derived(
		pendingAction?.kind === 'invite'
			? t('app.admin.invite_confirm_title')
			: pendingAction?.kind === 'reveal'
				? t('app.admin.new_link_confirm_title')
				: t('app.admin.revoke_confirm_title')
	);
	const confirmationText = $derived.by(() => {
		const action = pendingAction;
		if (action === null) return '';
		// A function replacement: an email may contain $, which a string
		// replacement would read as a substitution pattern.
		const subject = action.kind === 'invite' ? email : action.email;
		const key =
			action.kind === 'invite'
				? 'app.admin.invite_confirm_text'
				: action.kind === 'reveal'
					? 'app.admin.new_link_confirm_text'
					: 'app.admin.revoke_confirm_text';
		return t(key).replace('{email}', () => subject);
	});
</script>

<div class="flex min-h-0 flex-col gap-4 overflow-auto pb-2">
	<Card.Root class="p-0 [--card-spacing:0]">
		<Card.Header class="border-border border-b px-4 py-3">
			<Card.Title class="text-base">{t('app.admin.invite_title')}</Card.Title>
			<Card.Description>{t('app.admin.invitations_sub')}</Card.Description>
		</Card.Header>
		<Card.Content class="flex flex-col gap-4 p-4">
			<form class="flex flex-wrap items-end gap-3" onsubmit={submitInvite}>
				<label class="flex min-w-56 flex-col gap-1 text-xs font-medium">
					{t('app.admin.invite_email_label')}
					<Input
						type="email"
						name="email"
						required
						autocomplete="off"
						spellcheck={false}
						data-testid="admin-invite-email"
						placeholder={t('app.admin.invite_email_label')}
						bind:value={email}
					/>
				</label>
				<label class="flex min-w-40 flex-col gap-1 text-xs font-medium">
					{t('app.admin.invite_role_label')}
					<select
						data-testid="admin-invite-role"
						class="border-input bg-background h-9 rounded-md border px-2 text-sm"
						bind:value={role}
					>
						<option value="viewer">{t('app.admin.role_viewer')}</option>
						<option value="user">{t('app.admin.role_user')}</option>
						<option value="admin">{t('app.admin.role_admin')}</option>
					</select>
				</label>
				<Button type="submit" data-testid="admin-invite-submit" disabled={submitting}>
					{submitting ? t('app.admin.saving') : t('app.admin.invite_submit')}
				</Button>
			</form>

			{#if formError}
				<div role="alert" class="text-destructive text-sm">
					<p>{formError}</p>
					{#if formRecentAuthHint}
						<p data-testid="admin-invite-recent-auth-hint">
							{t('app.admin.invite_recent_auth_hint')}
						</p>
					{/if}
				</div>
			{/if}

			{#if minted}
				<div
					data-testid="admin-invite-minted"
					class="border-border flex flex-col gap-2 rounded-md border p-3"
				>
					<p class="text-sm font-medium">{t('app.admin.invite_link_title')}</p>
					<div class="flex items-center gap-2">
						<input
							bind:this={linkField}
							data-testid="admin-invite-link"
							aria-label={t('app.admin.invite_link_title')}
							readonly
							class="border-input bg-background h-9 w-full rounded-md border px-2 text-sm"
							value={minted.link}
						/>
						<Button
							type="button"
							variant="outline"
							data-testid="admin-invite-copy"
							onclick={copyLink}
						>
							{t('app.admin.copy_link')}
						</Button>
					</div>
					<p class="text-muted-foreground text-xs" aria-live="polite">{linkCopyHint}</p>
					<p class="text-muted-foreground text-xs">
						{t('app.admin.invite_link_expires')}: {formatDateTime(minted.expires_at)}
					</p>
					<p class="text-muted-foreground text-xs">{t('app.admin.invite_link_note')}</p>
					<Button
						type="button"
						variant="ghost"
						size="sm"
						class="w-fit"
						data-testid="admin-invite-dismiss"
						onclick={clearMinted}
					>
						{t('app.admin.dismiss')}
					</Button>
				</div>
			{/if}
		</Card.Content>
	</Card.Root>

	<Card.Root class="p-0 [--card-spacing:0]">
		<Card.Header class="border-border border-b px-4 py-3">
			<Card.Title class="text-base">{t('app.admin.invitations_title')}</Card.Title>
		</Card.Header>
		<Card.Content class="p-0">
			{#if listLoading}
				<p role="status" class="text-muted-foreground p-4 text-sm">
					{t('app.admin.loading_invitations')}
				</p>
			{:else if listError}
				<p role="alert" class="text-destructive p-4 text-sm">{listError}</p>
			{:else if invitations.length === 0}
				<p class="text-muted-foreground p-4 text-sm">{t('app.admin.invitations_empty')}</p>
			{:else}
				<div class="divide-border divide-y">
					{#each invitations as invitation (invitation.id)}
						<div
							data-testid="admin-invitation-row"
							class="flex flex-wrap items-center justify-between gap-3 px-4 py-3"
						>
							<div class="flex min-w-0 flex-col gap-1">
								<span class="break-all text-sm font-medium">{invitation.email}</span>
								<span class="text-muted-foreground flex flex-wrap gap-x-2 text-xs">
									<span>{t(roleLabelKey(invitation.role))}</span>
									{#if invitationExpired(invitation.expires_at, Date.now())}
										<span class="text-destructive">{t('app.admin.invite_expired')}</span>
									{:else}
										<span
											>{t('app.admin.invite_link_expires')}: {formatDateTime(
												invitation.expires_at
											)}</span
										>
									{/if}
								</span>
							</div>
							<div class="flex gap-2">
								<!-- A new link keeps the invitation's expiry, so on an expired one
								     it would be dead on arrival: only Revoke frees the address. -->
								{#if !invitationExpired(invitation.expires_at, Date.now())}
									<Button
										variant="outline"
										size="sm"
										data-testid="admin-invite-reveal"
										onclick={() => requestReveal(invitation)}
									>
										{t('app.admin.new_link')}
									</Button>
								{/if}
								<Button
									variant="outline"
									size="sm"
									data-testid="admin-invite-revoke"
									onclick={() => requestRevoke(invitation)}
								>
									{t('app.admin.revoke')}
								</Button>
							</div>
						</div>
					{/each}
				</div>
			{/if}
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
			{#if confirmationError}
				<p role="alert" class="text-destructive mt-3 text-sm">{confirmationError}</p>
			{/if}
			<div class="mt-6 flex justify-end gap-2">
				<Button variant="outline" disabled={confirmationSubmitting} onclick={cancelConfirmation}>
					{t('app.admin.cancel')}
				</Button>
				<Button
					data-testid="admin-invite-confirm"
					disabled={confirmationSubmitting}
					onclick={confirmAction}
				>
					{confirmationSubmitting ? t('app.admin.saving') : t('app.admin.confirm')}
				</Button>
			</div>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
