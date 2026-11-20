<script lang="ts">
	import PasswordInput from '$lib/components/password-input.svelte';
	import FirstRunFrame from '$lib/components/first-run-frame.svelte';
	import { onMount } from 'svelte';
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import Seo from '$lib/components/Seo.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Field from '$lib/components/ui/field';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import ClockIcon from '@lucide/svelte/icons/clock';
	import { refusalKey } from '$lib/account-logic';
	import { apiFetch } from '$lib/api';
	import { parseAuthError } from '$lib/auth-flow';
	import { minutesLeft, setupExpiryFrom, setupStepFrom, setupTokenFromHash } from '$lib/setup-flow';
	import { t, passwordLengthMessage } from '$lib/i18n.svelte';
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';

	const landing = PUBLIC_SITE_MODE === 'landing';
	const releaseCommand =
		'env -u COMPOSE_PROJECT_NAME docker compose --env-file deploy/compose/.env -f deploy/compose/compose.yml exec -T api python -m app.operator setup-link';
	// How often the minutes line is refreshed. The deadline decides when the link is over.
	const EXPIRY_TICK_MS = 15_000;

	type Phase = 'loading' | 'instruction' | 'error' | 'decided' | 'choose' | 'done';
	type Mode = 'none' | 'accounts';

	let token = $state<string | null>(null);
	let refused = $state(false);
	let phase = $state<Phase>('loading');
	// Off when an override already decided the mode: the claim is then the only step.
	let choiceOffered = $state(true);
	// Null until the user picks an option, so nothing is preselected.
	let mode = $state<Mode | null>(null);
	let email = $state('');
	let password = $state('');
	let confirmPassword = $state('');
	let error = $state('');
	let submitting = $state(false);
	// performance.now() time when the link stops working. Null when the API sent no expires_in.
	let deadline = $state<number | null>(null);
	let expiryMinutes = $state<number | null>(null);
	// Bumped by every hashchange. An answer that was asked for under an earlier
	// number belongs to a link the page has left, so it is dropped, even when the
	// link pasted again is the same one. Plain, not reactive: nothing renders it.
	let generation = 0;
	// The generation whose POST is in flight. It is set before the first await,
	// so a second click on either choice finds it and posts nothing.
	let inFlight: number | null = null;

	const headingText = $derived.by(() => {
		if (phase !== 'choose') return t('setup.title');
		return choiceOffered ? t('setup.choose_title') : t('setup.claim_title');
	});
	const description = $derived.by(() => {
		if (landing) return t('auth.landing.unavailable');
		if (phase === 'instruction') return refused ? t('setup.refused') : t('setup.missing');
		if (phase === 'error') return t('setup.config_failed');
		if (phase === 'decided') return t('setup.decided');
		if (phase === 'done') return t('setup.done');
		if (phase === 'choose') return choiceOffered ? t('setup.sub') : t('setup.accounts_hint');
		return undefined;
	});
	const expiryText = $derived(
		expiryMinutes === null ? null : t('setup.expires').replace('{minutes}', String(expiryMinutes))
	);
	const submitLabel = $derived.by(() => {
		if (submitting) return t('auth.submitting');
		if (!choiceOffered || mode === 'accounts') return t('setup.accounts_submit');
		if (mode === 'none') return t('setup.none_confirm');
		return t('setup.continue');
	});

	// The minutes line refreshes only while the choice is on screen.
	$effect(() => {
		if (phase !== 'choose' || deadline === null) return;
		const timer = setInterval(refreshExpiry, EXPIRY_TICK_MS);
		return () => clearInterval(timer);
	});

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
		mode = null;
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
			let expires: number | null = null;
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
				expires = setupExpiryFrom(result);
			}
			choiceOffered = step === 'choose';
			phase = step === 'decided' ? 'decided' : 'choose';
			startExpiry(step === 'decided' ? null : expires);
		} catch {
			if (isCurrent(attempt)) phase = 'error';
		}
	}

	// The deadline is set after the phase, so a link that is already out of time
	// goes straight to the instruction and is not overwritten by the choice.
	function startExpiry(seconds: number | null) {
		deadline = seconds === null ? null : performance.now() + seconds * 1000;
		refreshExpiry();
	}

	function refreshExpiry() {
		if (deadline === null) {
			expiryMinutes = null;
			return;
		}
		const secondsLeft = Math.ceil((deadline - performance.now()) / 1000);
		if (secondsLeft <= 0) {
			refuse();
			return;
		}
		expiryMinutes = minutesLeft(secondsLeft);
	}

	// A refused link gets the instruction and nothing else: the form goes, and
	// the link is not kept in memory for another try.
	function refuse() {
		token = null;
		refused = true;
		password = '';
		confirmPassword = '';
		deadline = null;
		expiryMinutes = null;
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
				void goto(resolve('/app'));
				return;
			}
			if (response.status === 403) {
				const parsed = await parseAuthError(response);
				if (!isCurrent(attempt)) return;
				if (parsed.kind === 'origin_not_allowed') {
					error = t('auth.error.origin_not_allowed');
					return;
				}
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

	// One submit for every choice. Nothing is posted before an option is picked,
	// and the account fields are checked before they are sent.
	async function submitForm(event: SubmitEvent) {
		event.preventDefault();
		if (choiceOffered && mode === null) return;
		if (choiceOffered && mode === 'none') {
			await choose({ mode: 'none' });
			return;
		}
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

{#snippet heading()}
	<h1 class="text-2xl font-semibold">{headingText}</h1>
{/snippet}

{#snippet expiry()}
	<p class="text-muted-foreground flex items-center gap-2 text-sm">
		<ClockIcon size={16} aria-hidden="true" />
		<span>{expiryText}</span>
	</p>
{/snippet}

<main class="bg-background text-foreground flex min-h-dvh items-center justify-center p-4">
	<div class="bg-card ring-foreground/10 w-full max-w-3xl rounded-xl p-6 ring-1 sm:p-8">
		<FirstRunFrame
			step={1}
			{heading}
			{description}
			footer={phase === 'choose' && expiryText !== null ? expiry : undefined}
		>
			{#if landing}
				<!-- The landing build shows only the heading and the unavailable text. -->
			{:else if phase === 'loading'}
				<p class="text-muted-foreground text-sm">{t('auth.loading')}</p>
			{:else if phase === 'instruction'}
				<div class="flex flex-col gap-4 text-sm">
					<div class="flex flex-col gap-2">
						<p>{t('setup.source_command')}</p>
						<pre
							class="bg-muted overflow-x-auto rounded px-3 py-2 font-mono text-xs whitespace-pre-wrap">make setup-link</pre>
					</div>
					<div class="flex flex-col gap-2">
						<p>{t('setup.release_command')}</p>
						<pre
							class="bg-muted overflow-x-auto rounded px-3 py-2 font-mono text-xs whitespace-pre-wrap">{#each releaseCommand.split(' ') as word, index (index)}{#if index > 0}{' '}{/if}<span
									class="whitespace-nowrap">{word}</span
								>{/each}</pre>
					</div>
				</div>
			{:else if phase === 'error'}
				<Button onclick={() => loadConfig()}>{t('setup.retry')}</Button>
			{:else if phase === 'decided'}
				<Button href={resolve('/app')}>{t('setup.open_studio')}</Button>
			{:else if phase === 'choose'}
				<form class="flex flex-col gap-6" onsubmit={submitForm}>
					{#if choiceOffered}
						<fieldset class="grid gap-3 sm:grid-cols-2">
							<legend class="sr-only">{t('setup.choose_title')}</legend>
							<label
								class="border-border has-checked:border-foreground has-checked:ring-foreground flex cursor-pointer flex-col gap-2 rounded-xl border p-4 has-checked:ring-1"
							>
								<span class="flex items-center justify-between gap-3">
									<span class="font-medium">{t('setup.choice_none_title')}</span>
									<input
										type="radio"
										name="setup-mode"
										value="none"
										class="size-4"
										bind:group={mode}
										onchange={() => (error = '')}
									/>
								</span>
								<span class="text-muted-foreground text-sm">{t('setup.choice_none_text')}</span>
							</label>
							<label
								class="border-border has-checked:border-foreground has-checked:ring-foreground flex cursor-pointer flex-col gap-2 rounded-xl border p-4 has-checked:ring-1"
							>
								<span class="flex items-center justify-between gap-3">
									<span class="font-medium">{t('setup.choice_accounts_title')}</span>
									<input
										type="radio"
										name="setup-mode"
										value="accounts"
										class="size-4"
										bind:group={mode}
										onchange={() => (error = '')}
									/>
								</span>
								<span class="text-muted-foreground text-sm">{t('setup.choice_accounts_text')}</span>
							</label>
						</fieldset>
					{/if}
					{#if !choiceOffered || mode === 'accounts'}
						<div class="flex flex-col gap-4">
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
						</div>
					{:else if mode === 'none'}
						<p class="text-muted-foreground text-sm">{t('setup.none_warning')}</p>
					{/if}
					{#if error}
						<p class="text-destructive text-sm" role="alert">{error}</p>
					{/if}
					<Button type="submit" disabled={submitting || (choiceOffered && mode === null)}>
						{submitLabel}
					</Button>
				</form>
			{:else if phase === 'done'}
				<div class="flex flex-col gap-4">
					<p class="text-muted-foreground text-sm">{t('setup.opening')}</p>
					<Button href={resolve('/app')}>{t('setup.open_studio')}</Button>
				</div>
			{/if}
		</FirstRunFrame>
	</div>
</main>
