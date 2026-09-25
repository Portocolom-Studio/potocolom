<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { apiFetch } from '$lib/api';
	import { createSubmitGuard, parseAuthError, readInviteTokenFromHash } from '$lib/auth-flow';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';

	let { recovery = false }: { recovery?: boolean } = $props();

	const askGuard = createSubmitGuard();
	const completeGuard = createSubmitGuard();

	let ready = $state(false);
	let token = $state<string | null>(null);
	let email = $state('');
	let password = $state('');
	let confirmPassword = $state('');
	let asked = $state(false);
	let invalid = $state(false);
	let error = $state('');
	let submitting = $state(false);

	onMount(() => {
		onHashChange();
		window.addEventListener('hashchange', onHashChange);
		ready = true;
		return () => window.removeEventListener('hashchange', onHashChange);
	});

	function onHashChange() {
		// A link pasted into an already-open tab changes only the fragment, so
		// the form follows it rather than waiting for a reload. The password
		// fields reset too, so a password typed for one link is not carried
		// into another.
		token = readInviteTokenFromHash(location.hash);
		asked = false;
		invalid = false;
		error = '';
		submitting = false;
		password = '';
		confirmPassword = '';
	}

	async function submitAsk(event: SubmitEvent) {
		event.preventDefault();
		await askGuard.run(async () => {
			error = '';
			submitting = true;
			try {
				const response = await apiFetch('/api/v1/auth/reset', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ email })
				});
				// The route answers the same whoever asked, and so does this
				// form, so the two cases can never be told apart.
				if (response.status === 202) {
					asked = true;
				} else {
					error = t('auth.error.generic');
				}
			} finally {
				submitting = false;
			}
		});
	}

	async function submitComplete(event: SubmitEvent) {
		event.preventDefault();
		await completeGuard.run(async () => {
			error = '';
			invalid = false;
			if (password !== confirmPassword) {
				error = t('auth.reset.password_mismatch');
				return;
			}
			submitting = true;
			try {
				const response = await apiFetch('/api/v1/auth/reset/complete', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ token, password })
				});
				if (response.status === 204) {
					// The reset returned no session, so the account holder
					// lands on the login screen, which shows the confirmation.
					// Replacing the entry drops the spent /reset#<token> from
					// history, so Back does not resurrect it.
					await goto(`${resolve('/login')}?reset=done`, { replaceState: true });
					return;
				}
				const parsed = await parseAuthError(response);
				if (parsed.kind === 'policy') {
					error = t('auth.reset.policy');
				} else if (parsed.kind === 'invalid') {
					invalid = true;
					error = recovery ? t('auth.recover.invalid') : t('auth.reset.invalid');
				} else {
					error = t('auth.error.generic');
				}
			} finally {
				submitting = false;
			}
		});
	}

	function askForNewLink() {
		location.hash = '';
	}
</script>

{#if !ready}
	<p class="text-muted-foreground text-center text-sm">{t('auth.loading')}</p>
{:else if !token}
	{#if recovery}
		<Card.Root>
			<Card.Header>
				<Card.Title>{t('auth.recover.title')}</Card.Title>
				<Card.Description>{t('auth.recover.missing_token')}</Card.Description>
			</Card.Header>
		</Card.Root>
	{:else if asked}
		<Card.Root>
			<Card.Header>
				<Card.Title>{t('auth.reset.title')}</Card.Title>
				<Card.Description role="status">{t('auth.reset.asked')}</Card.Description>
			</Card.Header>
		</Card.Root>
	{:else}
		<Card.Root>
			<Card.Header>
				<Card.Title>{t('auth.reset.title')}</Card.Title>
				<Card.Description>{t('auth.reset.ask_sub')}</Card.Description>
			</Card.Header>
			<Card.Content>
				<form class="flex flex-col gap-4" onsubmit={submitAsk}>
					<Field.Field>
						<Label for="reset-email">{t('auth.reset.email_label')}</Label>
						<Input
							id="reset-email"
							name="email"
							type="email"
							autocomplete="email"
							required
							bind:value={email}
							aria-label={t('auth.reset.email_label')}
						/>
					</Field.Field>
					{#if error}
						<p class="text-destructive text-sm" role="alert">{error}</p>
					{/if}
					<Button type="submit" disabled={submitting || !email.trim()}>
						{submitting ? t('auth.submitting') : t('auth.reset.ask_submit')}
					</Button>
				</form>
			</Card.Content>
		</Card.Root>
	{/if}
{:else}
	<Card.Root>
		<Card.Header>
			<Card.Title>{recovery ? t('auth.recover.title') : t('auth.reset.title')}</Card.Title>
			<Card.Description>
				{recovery ? t('auth.recover.complete_sub') : t('auth.reset.complete_sub')}
			</Card.Description>
		</Card.Header>
		<Card.Content>
			<form class="flex flex-col gap-4" onsubmit={submitComplete}>
				<Field.Field>
					<Label for="reset-password">{t('auth.reset.password_label')}</Label>
					<Input
						id="reset-password"
						name="password"
						type="password"
						autocomplete="new-password"
						required
						bind:value={password}
						aria-label={t('auth.reset.password_label')}
					/>
				</Field.Field>
				<Field.Field>
					<Label for="reset-confirm">{t('auth.reset.confirm_label')}</Label>
					<Input
						id="reset-confirm"
						name="confirm"
						type="password"
						autocomplete="new-password"
						required
						bind:value={confirmPassword}
						aria-label={t('auth.reset.confirm_label')}
					/>
				</Field.Field>
				{#if error}
					<p class="text-destructive text-sm" role="alert">{error}</p>
				{/if}
				<Button
					type="submit"
					disabled={submitting || !password || !confirmPassword}
					aria-label={t('auth.reset.submit')}
				>
					{submitting ? t('auth.submitting') : t('auth.reset.submit')}
				</Button>
				{#if invalid && !recovery}
					<Button type="button" variant="ghost" onclick={askForNewLink}>
						{t('auth.reset.ask_new')}
					</Button>
				{/if}
			</form>
		</Card.Content>
	</Card.Root>
{/if}
