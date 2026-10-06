<script lang="ts">
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import { deleteConfirmed, signInAgainHref } from '$lib/account-logic';
	import { accountRoleLabelKey, type Role } from '$lib/account-display';
	import { adminErrorMessage } from '$lib/studio-admin-logic';
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
	import { Dialog } from 'bits-ui';

	type AccountSession = {
		id: string;
		current: boolean;
		created_at: string;
		last_seen_at: string;
	};
	type AccountDetail = {
		id: string;
		email: string;
		role: Role;
		mail_verified: boolean;
		recent_auth: boolean;
		sessions: AccountSession[];
	};

	// The view reloads after a session ends, so `loading` covers only the first
	// answer: a refresh keeps the sections on screen instead of blanking them.
	let loading = $state(true);
	let detail = $state<AccountDetail | null>(null);
	let loadError = $state('');
	let sessionError = $state('');
	let endingSession = $state<string | null>(null);
	let currentPassword = $state('');
	let newPassword = $state('');
	let passwordError = $state('');
	let passwordNotice = $state('');
	let savingPassword = $state(false);
	let exportError = $state('');
	let exporting = $state(false);
	let deleteOpen = $state(false);
	let deleteTyped = $state('');
	let deleteError = $state('');
	let deleting = $state(false);

	const deleteReady = $derived(detail !== null && deleteConfirmed(deleteTyped, detail.email));

	function displayDate(value: string): string {
		const parsed = Date.parse(value);
		return Number.isFinite(parsed) ? new Date(parsed).toLocaleString() : value;
	}

	// The server's own detail explains a refusal (a wrong current password, a
	// weak one); the fallback covers an answer with no body.
	async function messageFor(response: Response): Promise<string> {
		const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
		return adminErrorMessage(body?.detail, t('app.account.request_failed'));
	}

	async function loadAccount(): Promise<void> {
		loadError = '';
		try {
			const response = await apiFetch('/api/v1/account');
			if (!response.ok) {
				loadError = await messageFor(response);
				detail = null;
				return;
			}
			detail = (await response.json()) as AccountDetail;
		} catch {
			loadError = t('app.account.request_failed');
			detail = null;
		} finally {
			loading = false;
		}
	}

	async function endSession(session: AccountSession): Promise<void> {
		if (endingSession !== null) return;
		endingSession = session.id;
		sessionError = '';
		try {
			const response = await apiFetch(
				`/api/v1/account/sessions/${encodeURIComponent(session.id)}`,
				{ method: 'DELETE' }
			);
			if (!response.ok) {
				sessionError = await messageFor(response);
				return;
			}
			// Ending this one cleared its cookies, so there is nothing left to read.
			if (session.current) {
				await goto(resolve('/login'));
				return;
			}
			await loadAccount();
		} catch {
			sessionError = t('app.account.request_failed');
		} finally {
			endingSession = null;
		}
	}

	async function savePassword(event?: SubmitEvent): Promise<void> {
		event?.preventDefault();
		if (detail === null || !detail.recent_auth || savingPassword) return;
		passwordNotice = '';
		passwordError = '';
		savingPassword = true;
		try {
			const response = await apiFetch('/api/v1/account/password', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ password: newPassword, current_password: currentPassword })
			});
			if (!response.ok) {
				passwordError = await messageFor(response);
				return;
			}
			passwordNotice = t('app.account.password_saved');
			currentPassword = '';
			newPassword = '';
		} catch {
			passwordError = t('app.account.request_failed');
		} finally {
			savingPassword = false;
		}
	}

	// The session is too old to change a password; a fresh one is what the
	// server asks for, so the way out is a new sign-in that returns here.
	async function signInAgain(): Promise<void> {
		try {
			await apiFetch('/api/v1/auth/logout', { method: 'POST' });
		} catch {
			// A sign-in page that finds a live session is still a sign-in page.
		}
		await goto(signInAgainHref());
	}

	async function exportAccount(): Promise<void> {
		exportError = '';
		exporting = true;
		try {
			const response = await apiFetch('/api/v1/account/export');
			if (!response.ok) {
				exportError = await messageFor(response);
				return;
			}
			const blob = await response.blob();
			const url = URL.createObjectURL(blob);
			const anchor = document.createElement('a');
			anchor.href = url;
			anchor.download = 'potocolom-account.json';
			document.body.appendChild(anchor);
			anchor.click();
			anchor.remove();
			// Some browsers cancel a download whose URL is revoked in the same tick.
			setTimeout(() => URL.revokeObjectURL(url), 1000);
		} catch {
			exportError = t('app.account.request_failed');
		} finally {
			exporting = false;
		}
	}

	function openDeleteDialog(): void {
		deleteTyped = '';
		deleteError = '';
		deleteOpen = true;
	}

	function cancelDelete(): void {
		deleteOpen = false;
		deleteTyped = '';
		deleteError = '';
	}

	async function deleteAccount(): Promise<void> {
		if (detail === null || !deleteReady || deleting) return;
		deleting = true;
		deleteError = '';
		try {
			const response = await apiFetch('/api/v1/account', { method: 'DELETE' });
			if (!response.ok) {
				deleteError = await messageFor(response);
				return;
			}
			await goto(resolve('/login'));
		} catch {
			deleteError = t('app.account.request_failed');
		} finally {
			deleting = false;
		}
	}

	onMount(() => {
		void loadAccount();
	});
</script>

<div class="no-scrollbar h-full overflow-y-auto">
	<div class="mx-auto flex w-full max-w-3xl flex-col gap-4 pb-4">
		<div>
			<h1 class="text-xl font-semibold">{t('app.account.title')}</h1>
			<p class="text-muted-foreground mt-1 text-sm leading-relaxed">{t('app.account.sub')}</p>
		</div>

		{#if loading}
			<p class="text-muted-foreground text-sm">{t('app.account.loading')}</p>
		{:else if loadError}
			<p role="alert" class="text-destructive text-sm">{loadError}</p>
		{:else if detail}
			<Card.Root class="p-0 [--card-spacing:0]">
				<Card.Header class="border-border border-b px-4 py-3">
					<Card.Title class="text-base">{t('app.account.profile')}</Card.Title>
				</Card.Header>
				<Card.Content class="flex flex-col gap-3 p-4">
					<dl class="grid gap-3 text-sm sm:grid-cols-2">
						<div>
							<dt class="text-muted-foreground text-xs">{t('app.account.email')}</dt>
							<dd class="mt-1 break-all font-medium">{detail.email}</dd>
						</div>
						<div>
							<dt class="text-muted-foreground text-xs">{t('app.account.role')}</dt>
							<dd class="mt-1 font-medium">{t(accountRoleLabelKey(detail.role))}</dd>
						</div>
					</dl>
					<p class="text-muted-foreground text-sm">
						{detail.mail_verified ? t('app.account.verified') : t('app.account.not_verified')}
					</p>
				</Card.Content>
			</Card.Root>

			<Card.Root class="p-0 [--card-spacing:0]">
				<Card.Header class="border-border border-b px-4 py-3">
					<Card.Title class="text-base">{t('app.account.sessions')}</Card.Title>
					<Card.Description>{t('app.account.sessions_sub')}</Card.Description>
				</Card.Header>
				<Card.Content class="p-4">
					{#if sessionError}
						<p role="alert" class="text-destructive mb-3 text-sm">{sessionError}</p>
					{/if}
					{#if detail.sessions.length === 0}
						<p class="text-muted-foreground text-sm">{t('app.account.sessions_empty')}</p>
					{:else}
						<ul class="divide-border divide-y">
							{#each detail.sessions as session (session.id)}
								<li class="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3">
									<div class="min-w-0">
										<p class="text-sm font-medium">
											{#if session.current}
												{t('app.account.this_device')}
											{:else}
												<span class="font-mono text-xs break-all">{session.id}</span>
											{/if}
										</p>
										<p class="text-muted-foreground flex flex-wrap gap-x-3 text-xs">
											<span>{t('app.account.created')}: {displayDate(session.created_at)}</span>
											<span>
												{t('app.account.last_seen')}: {displayDate(session.last_seen_at)}
											</span>
										</p>
									</div>
									<Button
										variant="outline"
										size="sm"
										disabled={endingSession !== null}
										onclick={() => void endSession(session)}
									>
										{t('app.account.sign_out')}
									</Button>
								</li>
							{/each}
						</ul>
					{/if}
				</Card.Content>
			</Card.Root>

			<Card.Root class="p-0 [--card-spacing:0]">
				<Card.Header class="border-border border-b px-4 py-3">
					<Card.Title class="text-base">{t('app.account.password')}</Card.Title>
					<Card.Description>{t('app.account.password_sub')}</Card.Description>
				</Card.Header>
				<Card.Content class="flex flex-col gap-3 p-4">
					{#if passwordNotice}
						<p role="status" class="text-muted-foreground text-sm">{passwordNotice}</p>
					{/if}
					{#if !detail.recent_auth}
						<div class="border-border bg-muted/30 rounded-lg border px-4 py-3 text-sm">
							<p>{t('app.account.recent_auth_note')}</p>
							<Button class="mt-2" variant="outline" size="sm" onclick={() => void signInAgain()}>
								{t('app.account.sign_in_again')}
							</Button>
						</div>
					{/if}
					<form class="grid gap-3 sm:grid-cols-2" onsubmit={savePassword}>
						<label class="flex flex-col gap-1 text-xs font-medium">
							{t('app.account.current_password')}
							<Input
								type="password"
								autocomplete="current-password"
								disabled={!detail.recent_auth || savingPassword}
								bind:value={currentPassword}
							/>
						</label>
						<label class="flex flex-col gap-1 text-xs font-medium">
							{t('app.account.new_password')}
							<Input
								type="password"
								autocomplete="new-password"
								disabled={!detail.recent_auth || savingPassword}
								bind:value={newPassword}
							/>
						</label>
						<div class="flex items-end sm:col-span-2">
							<Button type="submit" disabled={!detail.recent_auth || savingPassword}>
								{savingPassword ? t('app.account.saving') : t('app.account.save_password')}
							</Button>
						</div>
					</form>
					{#if passwordError}
						<p role="alert" class="text-destructive text-sm">{passwordError}</p>
					{/if}
				</Card.Content>
			</Card.Root>

			<Card.Root class="p-0 [--card-spacing:0]">
				<Card.Header class="border-border border-b px-4 py-3">
					<div class="flex flex-wrap items-center justify-between gap-3">
						<div>
							<Card.Title class="text-base">{t('app.account.your_data')}</Card.Title>
							<Card.Description>{t('app.account.your_data_sub')}</Card.Description>
						</div>
						<Button variant="outline" size="sm" disabled={exporting} onclick={exportAccount}>
							{t('app.account.download')}
						</Button>
					</div>
				</Card.Header>
				{#if exportError}
					<Card.Content class="p-4">
						<p role="alert" class="text-destructive text-sm">{exportError}</p>
					</Card.Content>
				{/if}
			</Card.Root>

			<Card.Root class="p-0 [--card-spacing:0]">
				<Card.Header class="border-border border-b px-4 py-3">
					<Card.Title class="text-base">{t('app.account.delete_title')}</Card.Title>
					<Card.Description>{t('app.account.delete_sub')}</Card.Description>
				</Card.Header>
				<Card.Content class="p-4">
					<Button variant="destructive" onclick={openDeleteDialog}>
						{t('app.account.delete_button')}
					</Button>
				</Card.Content>
			</Card.Root>
		{/if}
	</div>
</div>

<Dialog.Root bind:open={deleteOpen}>
	<Dialog.Portal>
		<Dialog.Overlay class="bg-background/80 fixed inset-0 z-50 backdrop-blur-sm" />
		<Dialog.Content
			class="bg-popover text-popover-foreground fixed top-1/2 left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border p-6 shadow-lg"
		>
			<Dialog.Title class="text-lg font-semibold">{t('app.account.delete_title')}</Dialog.Title>
			<Dialog.Description class="text-muted-foreground mt-2 text-sm">
				{t('app.account.delete_explain')}
			</Dialog.Description>
			<label class="mt-4 flex flex-col gap-1 text-xs font-medium">
				{t('app.account.delete_confirm_label')}
				<Input type="text" autocomplete="off" bind:value={deleteTyped} />
			</label>
			{#if deleteError}
				<p role="alert" class="text-destructive mt-3 text-sm">{deleteError}</p>
			{/if}
			<div class="mt-6 flex justify-end gap-2">
				<Button variant="outline" disabled={deleting} onclick={cancelDelete}>
					{t('app.account.cancel')}
				</Button>
				<Button variant="destructive" disabled={deleting || !deleteReady} onclick={deleteAccount}>
					{deleting ? t('app.account.saving') : t('app.account.delete_confirm')}
				</Button>
			</div>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
