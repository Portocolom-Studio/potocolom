<script lang="ts">
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { onDestroy, onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import {
		canUnlinkIdentity,
		deleteConfirmed,
		isFollowableRedirect,
		linkableProviders,
		refusalKey,
		signInAgainHref
	} from '$lib/account-logic';
	import { account } from '$lib/account.svelte';
	import { accountRoleLabelKey, type Role } from '$lib/account-display';
	import { adminErrorMessage, isRecentAuthenticationRequired } from '$lib/studio-admin-logic';
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
	import { Dialog } from 'bits-ui';

	type AccountSession = {
		id: string;
		current: boolean;
		created_at: string;
		last_seen_at: string | null;
	};
	type AccountDetail = {
		id: string;
		email: string;
		role: Role;
		mail_verified: boolean;
		recent_auth: boolean;
		totp: boolean;
		identities: string[];
		sessions: AccountSession[];
	};
	// What POST /api/v1/account/totp answers once, and what the view must not
	// hold on to: it is the only copy of the secret and of the recovery codes.
	type PendingSetup = {
		secret: string;
		uri: string;
		recovery_codes: string[];
		enrolment: string;
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
	let authMethods = $state<string[]>([]);
	let setup = $state<PendingSetup | null>(null);
	let replacing = $state(false);
	let setupCode = $state('');
	let currentCode = $state('');
	let setupBusy = $state(false);
	let setupError = $state('');
	let codesNotice = $state('');
	let codesError = $state('');
	let removeOpen = $state(false);
	let removeCode = $state('');
	let removeError = $state('');
	let removing = $state(false);
	let newEmail = $state('');
	let emailError = $state('');
	let emailNotice = $state('');
	let savingEmail = $state(false);
	let identityBusy = $state<string | null>(null);
	let identityError = $state('');
	// Set when the view unmounts, so a setup answer that lands after that
	// point is dropped instead of being written into a panel that is gone.
	let destroyed = false;
	// Bumped by every cancel, so a setup answer from before it is dropped.
	let setupGeneration = 0;

	const deleteReady = $derived(detail !== null && deleteConfirmed(deleteTyped, detail.email));
	const linkable = $derived(
		detail === null ? [] : linkableProviders(authMethods, detail.identities)
	);
	// Saving the address the account already holds would only reset the
	// verification the address already has, so the offer goes rather than
	// sending a no-op the server has to refuse. Trimmed and case-insensitive,
	// the way the server compares them.
	const emailUnchanged = $derived(
		detail !== null && newEmail.trim().toLowerCase() === detail.email.trim().toLowerCase()
	);
	// The remove dialog sits outside the section that only renders while the
	// account is on screen, so the condition the forms gate on is spelled once.
	const mayChange = $derived(detail !== null && detail.recent_auth);

	function displayDate(value: string | null): string {
		if (value === null) return '-';
		const parsed = Date.parse(value);
		return Number.isFinite(parsed) ? new Date(parsed).toLocaleString() : value;
	}

	// A 401 is not a refusal to report: the session is over, so the store
	// drops the account and this view leaves for the sign-in page instead of
	// writing an inline error nobody can answer. True when the caller must
	// stop, false when the response is an error worth showing.
	async function handleUnauthorized(response: Response): Promise<boolean> {
		if (response.status !== 401) return false;
		// The view is gone by then, and so is the reason to leave it: an
		// answer that lands after unmount must not drag whoever is now in
		// front of the screen off to the sign-in page.
		if (destroyed) return true;
		account.current = null;
		await goto(resolve('/login'));
		return true;
	}

	// A refusal this build has a translation for is shown in the reader's
	// language; anything else keeps the server's own text.
	function explain(body: { detail?: unknown } | null): string {
		const key = refusalKey(body?.detail);
		if (key !== null) return t(key);
		return adminErrorMessage(body?.detail, t('app.account.request_failed'));
	}

	// The server's own detail explains a refusal (a wrong current password, a
	// weak one); the fallback covers an answer with no body.
	async function messageFor(response: Response): Promise<string> {
		const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
		return explain(body);
	}

	// Every refusal is answered in the same order: a session that is over
	// leaves the view, a session too old for the change flips the flag that
	// shows the offer of a fresh sign-in and disables the form, with the raw
	// detail string kept out of the alert, and only what is left over reaches
	// the alert under the form that asked. `report` names which alert that is.
	async function refuse(response: Response, report: (message: string) => void): Promise<void> {
		if (await handleUnauthorized(response)) return;
		if (response.status === 403) {
			const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
			if (isRecentAuthenticationRequired(body?.detail)) {
				if (detail !== null) detail.recent_auth = false;
				return;
			}
			report(explain(body));
			return;
		}
		report(await messageFor(response));
	}

	// The account left this view, so a setup started against it goes with it:
	// the panel is unmounted and the secret must not wait there to reappear.
	function loseAccount(): void {
		detail = null;
		cancelSetup();
	}

	// A reload that fails keeps what is already on screen: the sections were
	// fine a moment ago and a failed read says nothing about them, so the
	// error goes beside them rather than in their place. Only the first load
	// has nothing to keep, and that one falls back to the error alone.
	async function loadAccount(): Promise<void> {
		const keeping = detail !== null;
		loadError = '';
		try {
			const response = await apiFetch('/api/v1/account');
			if (!response.ok) {
				if (await handleUnauthorized(response)) return;
				loadError = await messageFor(response);
				if (!keeping) loseAccount();
				return;
			}
			detail = (await response.json()) as AccountDetail;
		} catch {
			loadError = t('app.account.request_failed');
			if (!keeping) loseAccount();
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
				if (await handleUnauthorized(response)) return;
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
				await refuse(response, (message) => (passwordError = message));
				return;
			}
			// The change signs every other session out, so the list above has to
			// be read again before the notice claims it.
			await loadAccount();
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
				if (await handleUnauthorized(response)) return;
				exportError = await messageFor(response);
				return;
			}
			const blob = await response.blob();
			const url = URL.createObjectURL(blob);
			const anchor = document.createElement('a');
			anchor.href = url;
			anchor.download = 'potocolom-export.json';
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
				if (await handleUnauthorized(response)) return;
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

	// Two-factor sign-in. The server writes nothing until a code proves the
	// authenticator holds the secret, so this copy on screen is the only one
	// there is, and it goes as soon as the enrolment is done with it.
	async function beginSetup(mode: 'enrol' | 'replace'): Promise<void> {
		if (detail === null || !detail.recent_auth || setupBusy) return;
		const generation = setupGeneration;
		setupBusy = true;
		setupError = '';
		codesNotice = '';
		codesError = '';
		setupCode = '';
		currentCode = '';
		try {
			const response = await apiFetch('/api/v1/account/totp', { method: 'POST' });
			if (!response.ok) {
				await refuse(response, (message) => (setupError = message));
				return;
			}
			const pending = (await response.json()) as PendingSetup;
			// The answer can land after this view is gone, or after the setup
			// was cancelled or the account lost meanwhile, and the secret in it
			// is the only copy: nothing is written into state that late.
			if (destroyed || generation !== setupGeneration) return;
			setup = pending;
			replacing = mode === 'replace';
		} catch {
			setupError = t('app.account.request_failed');
		} finally {
			setupBusy = false;
		}
	}

	function cancelSetup(): void {
		setupGeneration += 1;
		setup = null;
		replacing = false;
		setupCode = '';
		currentCode = '';
		setupError = '';
		codesNotice = '';
		codesError = '';
	}

	async function confirmSetup(event?: SubmitEvent): Promise<void> {
		event?.preventDefault();
		if (detail === null || !detail.recent_auth || setup === null || setupBusy) return;
		const pending = setup;
		setupBusy = true;
		setupError = '';
		try {
			// Replacing also carries a code from the authenticator being
			// retired; a first enrolment has nothing to replace and is asked
			// for nothing beyond the code proving the new secret works.
			const body: Record<string, string> = {
				enrolment: pending.enrolment,
				code: setupCode.trim()
			};
			if (replacing) body.current_code = currentCode.trim();
			const response = await apiFetch('/api/v1/account/totp/confirm', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(body)
			});
			if (!response.ok) {
				// The setup stays open on purpose: the secret is still on
				// screen, so the next code needs no new enrolment.
				await refuse(response, (message) => (setupError = message));
				// Another tab or device may have confirmed a factor meanwhile,
				// or removed the one this replace was aimed at, and the refusal
				// is about what this account holds now: the re-read says so, so
				// a first enrolment then has to carry the code proving that
				// factor too, and a replace whose factor is gone is a first
				// enrolment again.
				await loadAccount();
				if (detail !== null) {
					if (detail.totp && !replacing) replacing = true;
					if (!detail.totp && replacing) replacing = false;
				}
				return;
			}
			// 204 means the factor stands. The secret, the enrolment and the
			// codes are spent, so none of them stays in the view.
			cancelSetup();
			await loadAccount();
		} catch {
			setupError = t('app.account.request_failed');
		} finally {
			setupBusy = false;
		}
	}

	// The codes are shown once and never again, so the copy goes through the
	// browser's own clipboard, and a refusal there is said rather than lost.
	async function copyCodes(): Promise<void> {
		if (setup === null) return;
		codesError = '';
		try {
			await navigator.clipboard.writeText(setup.recovery_codes.join('\n'));
			codesNotice = t('app.account.totp_copied');
		} catch {
			codesNotice = '';
			codesError = t('app.account.totp_copy_failed');
		}
	}

	function openRemoveDialog(): void {
		removeCode = '';
		removeError = '';
		removeOpen = true;
	}

	function cancelRemove(): void {
		removeOpen = false;
		removeCode = '';
		removeError = '';
	}

	async function removeFactor(): Promise<void> {
		if (detail === null || !detail.recent_auth || removing) return;
		const code = removeCode.trim();
		if (code === '') return;
		removeError = '';
		removing = true;
		try {
			const response = await apiFetch('/api/v1/account/totp', {
				method: 'DELETE',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ code })
			});
			if (!response.ok) {
				await refuse(response, (message) => (removeError = message));
				// A session too old for the change is answered behind this
				// dialog, and the note offering a fresh sign-in is on the
				// section underneath it: the dialog closes to let it show.
				if (detail !== null && !detail.recent_auth) cancelRemove();
				return;
			}
			cancelRemove();
			await loadAccount();
		} catch {
			removeError = t('app.account.request_failed');
		} finally {
			removing = false;
		}
	}

	async function saveEmail(event?: SubmitEvent): Promise<void> {
		event?.preventDefault();
		if (detail === null || !detail.recent_auth || savingEmail || emailUnchanged) return;
		const address = newEmail.trim();
		if (address === '') return;
		emailNotice = '';
		emailError = '';
		savingEmail = true;
		try {
			const response = await apiFetch('/api/v1/account/email', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ email: address })
			});
			if (!response.ok) {
				await refuse(response, (message) => (emailError = message));
				return;
			}
			// The change ends the other sessions and drops the assurance with
			// the old address, so the account is read again before the notice.
			await loadAccount();
			emailNotice = t('app.account.email_saved');
			newEmail = '';
		} catch {
			emailError = t('app.account.request_failed');
		} finally {
			savingEmail = false;
		}
	}

	async function unlinkIdentity(provider: string): Promise<void> {
		if (detail === null || !detail.recent_auth || identityBusy !== null) return;
		identityError = '';
		identityBusy = provider;
		try {
			const response = await apiFetch(
				`/api/v1/account/identities/${encodeURIComponent(provider)}`,
				{ method: 'DELETE' }
			);
			if (!response.ok) {
				await refuse(response, (message) => (identityError = message));
				return;
			}
			await loadAccount();
		} catch {
			identityError = t('app.account.request_failed');
		} finally {
			identityBusy = null;
		}
	}

	async function linkIdentity(provider: string): Promise<void> {
		if (detail === null || !detail.recent_auth || identityBusy !== null) return;
		identityError = '';
		identityBusy = provider;
		try {
			const response = await apiFetch(
				`/api/v1/account/identities/${encodeURIComponent(provider)}`,
				{ method: 'POST' }
			);
			if (!response.ok) {
				await refuse(response, (message) => (identityError = message));
				return;
			}
			const body = (await response.json()) as { redirect?: unknown };
			// The redirect is a navigation, so only an address the browser can
			// be sent to without question is followed: https anywhere, and
			// http on this very origin while developing locally.
			const redirect = typeof body.redirect === 'string' ? body.redirect : '';
			if (!isFollowableRedirect(redirect, location.origin)) {
				identityError = t('app.account.request_failed');
				return;
			}
			// The provider finishes the act on its own page; this view ends
			// with the navigation rather than with an error.
			location.assign(redirect);
		} catch {
			identityError = t('app.account.request_failed');
		} finally {
			identityBusy = null;
		}
	}

	// The names come from the dictionary, so both languages name the doors the
	// sign-in page offers. A provider this build does not name is shown as the
	// server spelled it rather than guessed at.
	function providerLabel(provider: string): string {
		if (provider === 'password') return t('app.account.identity_password');
		if (provider === 'google') return t('app.account.identity_google');
		if (provider === 'github') return t('app.account.identity_github');
		return provider;
	}

	// What this install offers to link is the list the sign-in page reads. One
	// read when the view opens; if it fails, the offer is simply not there
	// rather than an error under an account that is fine.
	async function loadAuthMethods(): Promise<void> {
		try {
			const response = await apiFetch('/api/v1/config');
			if (!response.ok) return;
			const config = (await response.json()) as { auth_methods?: string[] };
			authMethods = config.auth_methods ?? [];
		} catch {
			authMethods = [];
		}
	}

	onMount(() => {
		void loadAccount();
		void loadAuthMethods();
	});

	// The view is left with the setup still open more often than it completes,
	// and its secret is the only copy: dropping it here is what keeps it from
	// outliving the view that showed it.
	onDestroy(() => {
		destroyed = true;
		cancelSetup();
	});
</script>

{#snippet freshSignIn(noteKey: Parameters<typeof t>[0])}
	{#if detail !== null && !detail.recent_auth}
		<div class="border-border bg-muted/30 rounded-lg border px-4 py-3 text-sm">
			<p>{t(noteKey)}</p>
			<Button class="mt-2" variant="outline" size="sm" onclick={() => void signInAgain()}>
				{t('app.account.sign_in_again')}
			</Button>
		</div>
	{/if}
{/snippet}

<div class="no-scrollbar h-full overflow-y-auto">
	<div class="mx-auto flex w-full max-w-3xl flex-col gap-4 pb-4">
		<div>
			<h1 class="text-xl font-semibold">{t('app.account.title')}</h1>
			<p class="text-muted-foreground mt-1 text-sm leading-relaxed">{t('app.account.sub')}</p>
		</div>

		{#if loading}
			<p class="text-muted-foreground text-sm">{t('app.account.loading')}</p>
		{:else if detail}
			{#if loadError}
				<p role="alert" class="text-destructive text-sm">{loadError}</p>
			{/if}
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
										aria-label={t('app.account.sign_out_session')
											.replace('{date}', () => displayDate(session.created_at))
											.replace('{seen}', () => displayDate(session.last_seen_at))}
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
					{@render freshSignIn('app.account.recent_auth_note')}
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
							<Button
								type="submit"
								disabled={!detail.recent_auth || savingPassword || newPassword === ''}
							>
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
					<Card.Title class="text-base">{t('app.account.totp')}</Card.Title>
					<Card.Description>{t('app.account.totp_sub')}</Card.Description>
				</Card.Header>
				<Card.Content class="flex flex-col gap-3 p-4">
					{@render freshSignIn('app.account.recent_auth_needed')}
					<p class="text-sm">
						{detail.totp ? t('app.account.totp_on') : t('app.account.totp_off')}
					</p>
					{#if setup !== null}
						<form class="grid gap-3" onsubmit={confirmSetup}>
							<p class="text-sm font-medium">
								{replacing
									? t('app.account.totp_replace_heading')
									: t('app.account.totp_setup_heading')}
							</p>
							<label class="flex flex-col gap-1 text-xs font-medium">
								{t('app.account.totp_secret_label')}
								<Input type="text" readonly autocomplete="off" value={setup.secret} />
							</label>
							<p class="text-muted-foreground text-sm">{t('app.account.totp_secret_note')}</p>
							<a
								class="text-sm underline"
								href={setup.uri}
								target="_blank"
								rel="noopener noreferrer"
							>
								{t('app.account.totp_open_app')}
							</a>
							<div>
								<p class="text-xs font-medium">{t('app.account.totp_codes')}</p>
								<p class="text-muted-foreground mt-1 text-sm">
									{t('app.account.totp_codes_note')}
								</p>
								<ul class="mt-2 grid gap-1 font-mono text-sm">
									{#each setup.recovery_codes as code (code)}
										<li>{code}</li>
									{/each}
								</ul>
								<Button
									type="button"
									class="mt-2"
									variant="outline"
									size="sm"
									onclick={() => void copyCodes()}
								>
									{t('app.account.totp_copy_codes')}
								</Button>
								{#if codesNotice}
									<p role="status" class="text-muted-foreground mt-2 text-sm">{codesNotice}</p>
								{/if}
								{#if codesError}
									<p role="alert" class="text-destructive mt-2 text-sm">{codesError}</p>
								{/if}
							</div>
							<label class="flex flex-col gap-1 text-xs font-medium">
								{t('app.account.totp_code_label')}
								<Input
									type="text"
									inputmode="numeric"
									autocomplete="one-time-code"
									maxlength={6}
									disabled={!detail.recent_auth || setupBusy}
									bind:value={setupCode}
								/>
							</label>
							{#if replacing}
								<label class="flex flex-col gap-1 text-xs font-medium">
									{t('app.account.totp_current_code_label')}
									<!-- A recovery code carries letters, so the keyboard cannot be numeric. -->
									<Input
										type="text"
										inputmode="text"
										autocomplete="off"
										disabled={!detail.recent_auth || setupBusy}
										bind:value={currentCode}
									/>
								</label>
							{/if}
							<div class="flex flex-wrap gap-2">
								<Button
									type="submit"
									disabled={!detail.recent_auth ||
										setupBusy ||
										setupCode.trim() === '' ||
										(replacing && currentCode.trim() === '')}
								>
									{t('app.account.totp_confirm')}
								</Button>
								<Button type="button" variant="outline" disabled={setupBusy} onclick={cancelSetup}>
									{t('app.account.cancel')}
								</Button>
							</div>
						</form>
					{:else}
						<div class="flex flex-wrap gap-2">
							{#if !detail.totp}
								<Button
									disabled={!detail.recent_auth || setupBusy}
									onclick={() => void beginSetup('enrol')}
								>
									{t('app.account.totp_setup')}
								</Button>
							{:else}
								<Button
									variant="outline"
									disabled={!detail.recent_auth || setupBusy}
									onclick={() => void beginSetup('replace')}
								>
									{t('app.account.totp_replace')}
								</Button>
								<Button
									variant="destructive"
									disabled={!detail.recent_auth || removing}
									onclick={openRemoveDialog}
								>
									{t('app.account.totp_remove')}
								</Button>
							{/if}
						</div>
					{/if}
					{#if setupError}
						<p role="alert" class="text-destructive text-sm">{setupError}</p>
					{/if}
				</Card.Content>
			</Card.Root>

			<Card.Root class="p-0 [--card-spacing:0]">
				<Card.Header class="border-border border-b px-4 py-3">
					<Card.Title class="text-base">{t('app.account.email')}</Card.Title>
					<Card.Description>{t('app.account.email_sub')}</Card.Description>
				</Card.Header>
				<Card.Content class="flex flex-col gap-3 p-4">
					{@render freshSignIn('app.account.recent_auth_needed')}
					{#if emailNotice}
						<p role="status" class="text-muted-foreground text-sm">{emailNotice}</p>
					{/if}
					<p class="text-sm">
						<span class="text-muted-foreground text-xs">{t('app.account.email_current')}</span>
						<span class="ml-2 break-all font-medium">{detail.email}</span>
					</p>
					<form class="grid gap-3 sm:grid-cols-2" onsubmit={saveEmail}>
						<label class="flex flex-col gap-1 text-xs font-medium">
							{t('app.account.email_new')}
							<Input
								type="email"
								autocomplete="email"
								disabled={!detail.recent_auth || savingEmail}
								bind:value={newEmail}
							/>
						</label>
						<div class="flex items-end">
							<Button
								type="submit"
								disabled={!detail.recent_auth ||
									savingEmail ||
									emailUnchanged ||
									newEmail.trim() === ''}
							>
								{savingEmail ? t('app.account.saving') : t('app.account.save_email')}
							</Button>
						</div>
					</form>
					{#if emailError}
						<p role="alert" class="text-destructive text-sm">{emailError}</p>
					{/if}
				</Card.Content>
			</Card.Root>

			<Card.Root class="p-0 [--card-spacing:0]">
				<Card.Header class="border-border border-b px-4 py-3">
					<Card.Title class="text-base">{t('app.account.identities')}</Card.Title>
					<Card.Description>{t('app.account.identities_sub')}</Card.Description>
				</Card.Header>
				<Card.Content class="flex flex-col gap-3 p-4">
					{@render freshSignIn('app.account.recent_auth_needed')}
					{#if identityError}
						<p role="alert" class="text-destructive text-sm">{identityError}</p>
					{/if}
					<ul class="divide-border divide-y">
						{#each detail.identities as identity (identity)}
							<li class="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3">
								<p class="text-sm font-medium">{providerLabel(identity)}</p>
								{#if canUnlinkIdentity(identity, detail.identities)}
									<Button
										variant="outline"
										size="sm"
										disabled={!detail.recent_auth || identityBusy !== null}
										onclick={() => void unlinkIdentity(identity)}
									>
										{t('app.account.unlink')}
									</Button>
								{/if}
							</li>
						{/each}
					</ul>
					{#if linkable.length > 0}
						<div class="flex flex-wrap items-center gap-2 border-border border-t pt-3">
							<span class="text-muted-foreground text-sm">{t('app.account.link_offer')}</span>
							{#each linkable as provider (provider)}
								<Button
									variant="outline"
									size="sm"
									disabled={!detail.recent_auth || identityBusy !== null}
									onclick={() => void linkIdentity(provider)}
								>
									{t('app.account.link')}
									{providerLabel(provider)}
								</Button>
							{/each}
						</div>
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
		{:else if loadError}
			<p role="alert" class="text-destructive text-sm">{loadError}</p>
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
					{deleting ? t('app.account.deleting') : t('app.account.delete_confirm')}
				</Button>
			</div>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>

<Dialog.Root bind:open={removeOpen}>
	<Dialog.Portal>
		<Dialog.Overlay class="bg-background/80 fixed inset-0 z-50 backdrop-blur-sm" />
		<Dialog.Content
			class="bg-popover text-popover-foreground fixed top-1/2 left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border p-6 shadow-lg"
		>
			<Dialog.Title class="text-lg font-semibold">
				{t('app.account.totp_remove_title')}
			</Dialog.Title>
			<Dialog.Description class="text-muted-foreground mt-2 text-sm">
				{t('app.account.totp_remove_note')}
			</Dialog.Description>
			<label class="mt-4 flex flex-col gap-1 text-xs font-medium">
				{t('app.account.totp_current_code_label')}
				<!-- A recovery code carries letters, so the keyboard cannot be numeric. -->
				<Input
					type="text"
					inputmode="text"
					autocomplete="off"
					disabled={!mayChange || removing}
					bind:value={removeCode}
				/>
			</label>
			{#if removeError}
				<p role="alert" class="text-destructive mt-3 text-sm">{removeError}</p>
			{/if}
			<div class="mt-6 flex justify-end gap-2">
				<Button variant="outline" disabled={removing} onclick={cancelRemove}>
					{t('app.account.cancel')}
				</Button>
				<Button
					variant="destructive"
					disabled={!mayChange || removing || removeCode.trim() === ''}
					onclick={() => void removeFactor()}
				>
					{t('app.account.totp_remove')}
				</Button>
			</div>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
