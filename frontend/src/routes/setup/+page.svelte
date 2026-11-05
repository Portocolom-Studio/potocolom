<script lang="ts">
	import PasswordInput from '$lib/components/password-input.svelte';
	import { onMount } from 'svelte';
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import AuthSplit from '$lib/components/auth/AuthSplit.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { refusalKey } from '$lib/account-logic';
	import { apiFetch } from '$lib/api';
	import { parseAuthError } from '$lib/auth-flow';
	import { setupStepFrom, setupTokenFromHash, type SetupStep } from '$lib/setup-flow';
	import { t, passwordLengthMessage } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';

	const landing = PUBLIC_SITE_MODE === 'landing';
	const releaseCommand =
		'env -u COMPOSE_PROJECT_NAME docker compose --env-file deploy/compose/.env -f deploy/compose/compose.yml exec -T api python -m app.operator setup-link';
	const phaseFor: Record<SetupStep, Phase> = {
		choose: 'choose',
		claim: 'accounts',
		decided: 'decided'
	};

	type Phase =
		'loading' | 'instruction' | 'error' | 'decided' | 'choose' | 'accounts' | 'none' | 'done';

	let token = $state<string | null>(null);
	let refused = $state(false);
	let phase = $state<Phase>('loading');
	// Off when an override already decided the mode: the claim is then the only step.
	let choiceOffered = $state(true);
	let email = $state('');
	let password = $state('');
	let confirmPassword = $state('');
	let error = $state('');
	let submitting = $state(false);
	// Bumped by every hashchange. An answer that was asked for under an earlier
	// number belongs to a link the page has left, so it is dropped, even when the
	// link pasted again is the same one. Plain, not reactive: nothing renders it.
	let generation = 0;
	// The generation whose POST is in flight. It is set before the first await,
	// so a second click on either choice finds it and posts nothing.
	let inFlight: number | null = null;

	onMount(() => {
		if (landing) return;
		onHashChange();
		window.addEventListener('hashchange', onHashChange);
		return () => window.removeEventListener('hashchange', onHashChange);
	});

	function onHashChange() {
		generation += 1;
		// The fragment carries the one-use link. It leaves the address bar as
		// soon as it is read, and a link pasted into an open tab is taken the
		// same way. The native call, because the router's replaceState throws
		// until it has started, and onMount runs during hydration.
		token = setupTokenFromHash(location.hash);
		if (location.hash) {
			history.replaceState(history.state, '', `${location.pathname}${location.search}`);
		}
		refused = false;
		error = '';
		submitting = false;
		password = '';
		confirmPassword = '';
		if (token === null) {
			phase = 'instruction';
			return;
		}
		void loadConfig();
	}

	function isCurrent(attempt: number) {
		return attempt === generation && phase !== 'done';
	}

	// The token stays in memory when the read fails, so Retry asks again with
	// the same link. Nothing was posted, so the link is still unspent.
	async function loadConfig() {
		const attempt = generation;
		const link = token;
		if (link === null) return;
		phase = 'loading';
		try {
			const response = await apiFetch('/api/v1/config');
			if (!isCurrent(attempt)) return;
			if (!response.ok) {
				phase = 'error';
				return;
			}
			const body: unknown = await response.json();
			if (!isCurrent(attempt)) return;
			let step = setupStepFrom(body);
			if (step !== 'decided') {
				const status = await apiFetch('/api/v1/auth/setup/status', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ token: link })
				});
				if (!isCurrent(attempt)) return;
				if (status.status === 403) {
					refuse();
					return;
				}
				if (!status.ok) {
					phase = 'error';
					return;
				}
				const result: unknown = await status.json();
				if (!isCurrent(attempt)) return;
				if (
					typeof result !== 'object' ||
					result === null ||
					!('claimed' in result) ||
					typeof result.claimed !== 'boolean'
				) {
					phase = 'error';
					return;
				}
				step = result.claimed ? 'decided' : step;
			}
			choiceOffered = step === 'choose';
			phase = phaseFor[step];
		} catch {
			if (isCurrent(attempt)) phase = 'error';
		}
	}

	// A refused link gets the instruction and nothing else: the form goes, and
	// the link is not kept in memory for another try.
	function refuse() {
		token = null;
		refused = true;
		password = '';
		confirmPassword = '';
		phase = 'instruction';
	}

	// One POST for both choices. The answer decides what the page becomes: a
	// spent or expired link refuses, an install that is already decided says so,
	// and a bad email or weak password is shown beside the form. An answer for a
	// link the page has left, or after a success, changes nothing.
	async function choose(
		body: { mode: 'accounts'; email: string; password: string } | { mode: 'none' }
	) {
		const link = token;
		if (link === null || inFlight === generation) return;
		const attempt = generation;
		inFlight = attempt;
		error = '';
		submitting = true;
		try {
			const response = await apiFetch('/api/v1/auth/setup', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ token: link, ...body })
			});
			if (!isCurrent(attempt)) return;
			if (response.status === 204) {
				password = '';
				confirmPassword = '';
				phase = 'done';
				return;
			}
			if (response.status === 403) {
				refuse();
				return;
			}
			if (response.status === 409) {
				phase = 'decided';
				return;
			}
			if (response.status === 404) {
				await loadConfig();
				if (!isCurrent(attempt)) return;
				return;
			}
			const parsed = await parseAuthError(response);
			if (!isCurrent(attempt)) return;
			const key = refusalKey(parsed.message);
			error = key === null ? t('auth.error.generic') : t(key);
		} catch {
			if (isCurrent(attempt)) error = t('auth.error.generic');
		} finally {
			if (inFlight === attempt) inFlight = null;
			if (isCurrent(attempt)) submitting = false;
		}
	}

	async function submitAccounts(event: SubmitEvent) {
		event.preventDefault();
		error = '';
		error = passwordLengthMessage(password);
		if (error) return;
		if (password !== confirmPassword) {
			error = t('auth.join.password_mismatch');
			return;
		}
		await choose({ mode: 'accounts', email, password });
	}
</script>

<Seo titleKey="seo.setup_title" descriptionKey="seo.setup_description" path="/setup" noindex />

<AuthSplit fieldSide="end" heading={t('hero.title1')} headingTail={t('hero.title2')}>
	{#if landing}
		<Card.Root>
			<Card.Header>
				<h1 data-slot="card-title" class="text-base font-medium">{t('setup.title')}</h1>
				<Card.Description>{t('auth.landing.unavailable')}</Card.Description>
			</Card.Header>
		</Card.Root>
	{:else if phase === 'loading'}
		<p class="text-muted-foreground text-center text-sm">{t('auth.loading')}</p>
	{:else if phase === 'instruction'}
		<Card.Root>
			<Card.Header>
				<h1 data-slot="card-title" class="text-base font-medium">{t('setup.title')}</h1>
				<Card.Description>{refused ? t('setup.refused') : t('setup.missing')}</Card.Description>
			</Card.Header>
			<Card.Content class="flex flex-col gap-4 text-sm">
				<div class="flex flex-col gap-2">
					<p>{t('setup.source_command')}</p>
					<pre
						class="bg-muted overflow-x-auto rounded px-3 py-2 font-mono text-xs whitespace-pre-wrap">make setup-link</pre>
				</div>
				<div class="flex flex-col gap-2">
					<p>{t('setup.release_command')}</p>
					<pre
						class="bg-muted overflow-x-auto rounded px-3 py-2 font-mono text-xs whitespace-pre-wrap">{releaseCommand}</pre>
				</div>
			</Card.Content>
		</Card.Root>
	{:else if phase === 'error'}
		<Card.Root>
			<Card.Header>
				<h1 data-slot="card-title" class="text-base font-medium">{t('setup.title')}</h1>
				<Card.Description>{t('setup.config_failed')}</Card.Description>
			</Card.Header>
			<Card.Content>
				<Button onclick={() => loadConfig()}>{t('setup.retry')}</Button>
			</Card.Content>
		</Card.Root>
	{:else if phase === 'decided'}
		<Card.Root>
			<Card.Header>
				<h1 data-slot="card-title" class="text-base font-medium">{t('setup.title')}</h1>
				<Card.Description>{t('setup.decided')}</Card.Description>
			</Card.Header>
			<Card.Content>
				<Button href={resolve('/app')}>{t('setup.open_studio')}</Button>
			</Card.Content>
		</Card.Root>
	{:else if phase === 'choose'}
		<Card.Root>
			<Card.Header>
				<h1 data-slot="card-title" class="text-base font-medium">{t('setup.title')}</h1>
				<Card.Description>{t('setup.sub')}</Card.Description>
			</Card.Header>
			<Card.Content class="flex flex-col gap-6">
				<div class="flex flex-col gap-2">
					<p class="text-muted-foreground text-sm">{t('setup.accounts_hint')}</p>
					<Button onclick={() => (phase = 'accounts')}>{t('setup.accounts_title')}</Button>
				</div>
				<div class="flex flex-col gap-2">
					<p class="text-muted-foreground text-sm">{t('setup.none_hint')}</p>
					<Button variant="outline" onclick={() => (phase = 'none')}>{t('setup.none_title')}</Button
					>
				</div>
			</Card.Content>
		</Card.Root>
	{:else if phase === 'accounts'}
		<Card.Root>
			<Card.Header>
				<h1 data-slot="card-title" class="text-base font-medium">{t('setup.accounts_title')}</h1>
				<Card.Description>{t('setup.accounts_hint')}</Card.Description>
			</Card.Header>
			<Card.Content>
				<form class="flex flex-col gap-4" onsubmit={submitAccounts}>
					<Field.Field>
						<Label for="setup-email">{t('auth.login.email_label')}</Label>
						<Input
							id="setup-email"
							name="email"
							type="email"
							autocomplete="username"
							spellcheck={false}
							required
							bind:value={email}
						/>
					</Field.Field>
					<Field.Field>
						<Label for="setup-password">{t('auth.join.password_label')}</Label>
						<PasswordInput
							id="setup-password"
							name="password"
							autocomplete="new-password"
							hint
							required
							bind:value={password}
						/>
					</Field.Field>
					<Field.Field>
						<Label for="setup-confirm">{t('auth.join.confirm_label')}</Label>
						<PasswordInput
							id="setup-confirm"
							name="confirm"
							autocomplete="new-password"
							toggleLabel="auth.password.show_confirmation"
							required
							bind:value={confirmPassword}
						/>
					</Field.Field>
					{#if error}
						<p class="text-destructive text-sm" role="alert">{error}</p>
					{/if}
					<Button type="submit" disabled={submitting}>
						{submitting ? t('auth.submitting') : t('setup.accounts_submit')}
					</Button>
					{#if choiceOffered}
						<Button
							type="button"
							variant="ghost"
							onclick={() => {
								phase = 'choose';
								error = '';
							}}
						>
							{t('setup.back')}
						</Button>
					{/if}
				</form>
			</Card.Content>
		</Card.Root>
	{:else if phase === 'none'}
		<Card.Root>
			<Card.Header>
				<h1 data-slot="card-title" class="text-base font-medium">{t('setup.none_title')}</h1>
				<Card.Description>{t('setup.none_warning')}</Card.Description>
			</Card.Header>
			<Card.Content class="flex flex-col gap-4">
				{#if error}
					<p class="text-destructive text-sm" role="alert">{error}</p>
				{/if}
				<Button disabled={submitting} onclick={() => choose({ mode: 'none' })}>
					{submitting ? t('auth.submitting') : t('setup.none_confirm')}
				</Button>
				<Button
					type="button"
					variant="ghost"
					onclick={() => {
						phase = 'choose';
						error = '';
					}}
				>
					{t('setup.back')}
				</Button>
			</Card.Content>
		</Card.Root>
	{:else if phase === 'done'}
		<Card.Root>
			<Card.Header>
				<h1 data-slot="card-title" class="text-base font-medium">{t('setup.title')}</h1>
				<Card.Description>{t('setup.done')}</Card.Description>
			</Card.Header>
			<Card.Content class="flex flex-col gap-6">
				<div class="flex flex-col gap-2">
					<p class="text-muted-foreground text-sm">{t('setup.step_models')}</p>
					<a
						class="text-foreground text-sm underline underline-offset-4"
						href={`${resolve('/app')}?view=admin&tab=models`}>{t('setup.open_models')}</a
					>
				</div>
				<Button href={resolve('/app')}>{t('setup.open_studio')}</Button>
			</Card.Content>
		</Card.Root>
	{/if}
</AuthSplit>
