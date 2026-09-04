<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { apiFetch } from '$lib/api';
	import {
		createSubmitGuard,
		initialAuthView,
		parseAuthError,
		shouldShowChallenge,
		type AuthView
	} from '$lib/auth-flow';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';

	const landing = PUBLIC_SITE_MODE === 'landing';
	const loginGuard = createSubmitGuard();
	const challengeGuard = createSubmitGuard();

	let authMethods = $state<string[]>([]);
	let view = $state<AuthView>('password');
	let email = $state('');
	let password = $state('');
	let rememberMe = $state(false);
	let code = $state('');
	let error = $state('');
	let loadingConfig = $state(!landing);
	let submitting = $state(false);
	let challengeAttempts = $state(0);

	const showPasswordForm = $derived(!landing && authMethods.includes('password') && !shouldShowChallenge(view));
	const showChallengeForm = $derived(!landing && shouldShowChallenge(view));
	const showGoogle = $derived(!landing && authMethods.includes('google'));
	const showGithub = $derived(!landing && authMethods.includes('github'));

	onMount(async () => {
		if (landing) return;
		view = initialAuthView(page.url.search);
		try {
			const response = await apiFetch('/api/v1/config');
			if (!response.ok) throw new Error('config failed');
			const config = (await response.json()) as { auth_methods?: string[] };
			authMethods = config.auth_methods ?? [];
		} catch {
			error = t('auth.error.config');
		} finally {
			loadingConfig = false;
		}
	});

	async function submitLogin(event: SubmitEvent) {
		event.preventDefault();
		await loginGuard.run(async () => {
			error = '';
			submitting = true;
			try {
				const response = await apiFetch('/api/v1/auth/login', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ email, password, remember_me: rememberMe })
				});
				if (response.status === 204) {
					await goto(resolve('/app'));
					return;
				}
				if (response.status === 200) {
					const body = (await response.json()) as { totp_required?: boolean };
					if (body.totp_required) {
						view = 'challenge';
						code = '';
						challengeAttempts = 0;
						return;
					}
				}
				const parsed = await parseAuthError(response);
				error = messageFor(parsed);
			} finally {
				submitting = false;
			}
		});
	}

	async function submitChallenge(event: SubmitEvent) {
		event.preventDefault();
		await challengeGuard.run(async () => {
			error = '';
			submitting = true;
			try {
				const response = await apiFetch('/api/v1/auth/totp', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ code })
				});
				if (response.status === 204) {
					await goto(resolve('/app'));
					return;
				}
				const parsed = await parseAuthError(response);
				if (response.status === 403) {
					challengeAttempts += 1;
					if (challengeAttempts >= 10) {
						view = 'password';
						code = '';
						password = '';
						challengeAttempts = 0;
						error = t('auth.error.challenge_ended');
						return;
					}
					error = t('auth.challenge.invalid');
					return;
				}
				error = messageFor(parsed);
			} finally {
				submitting = false;
			}
		});
	}

	function messageFor(parsed: Awaited<ReturnType<typeof parseAuthError>>): string {
		if (parsed.kind === 'rate_limited') {
			const seconds = parsed.retryAfterSeconds;
			return seconds
				? t('auth.error.busy_seconds').replace('{seconds}', String(seconds))
				: t('auth.error.rate_limited');
		}
		if (parsed.kind === 'busy') {
			const seconds = parsed.retryAfterSeconds;
			return seconds ? t('auth.error.busy_seconds').replace('{seconds}', String(seconds)) : t('auth.error.busy');
		}
		if (parsed.kind === 'invalid') return t('auth.error.invalid');
		return t('auth.error.generic');
	}

	function backToPassword() {
		view = 'password';
		code = '';
		challengeAttempts = 0;
		error = '';
	}
</script>

<Seo
	title="Sign in | potocolom"
	description="Sign in to the potocolom studio."
	path="/login"
	noindex
/>

<div class="bg-background flex min-h-dvh flex-col">
	<header class="border-b">
		<div class="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-6">
			<a class="text-base font-bold tracking-tight" href={resolve('/')}>
				potocolom<span class="text-primary">_</span>
			</a>
			<LanguageToggle />
		</div>
	</header>

	<main class="mx-auto flex w-full max-w-md flex-1 flex-col justify-center px-4 py-12 sm:px-6">
		{#if landing}
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('auth.login.title')}</Card.Title>
					<Card.Description>{t('auth.landing.unavailable')}</Card.Description>
				</Card.Header>
			</Card.Root>
		{:else if loadingConfig}
			<p class="text-muted-foreground text-center text-sm">{t('auth.loading')}</p>
		{:else if showChallengeForm}
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('auth.challenge.title')}</Card.Title>
					<Card.Description>{t('auth.challenge.sub')}</Card.Description>
				</Card.Header>
				<Card.Content>
					<form class="flex flex-col gap-4" onsubmit={submitChallenge}>
						<Field.Field>
							<Label for="totp-code">{t('auth.challenge.code_label')}</Label>
							<Input
								id="totp-code"
								name="code"
								autocomplete="one-time-code"
								inputmode="text"
								required
								bind:value={code}
								aria-label={t('auth.challenge.code_label')}
							/>
							<Field.Description>{t('auth.challenge.code_hint')}</Field.Description>
						</Field.Field>
						{#if error}
							<p class="text-destructive text-sm" role="alert">{error}</p>
						{/if}
						<Button type="submit" disabled={submitting || !code.trim()}>
							{submitting ? t('auth.submitting') : t('auth.challenge.submit')}
						</Button>
						<Button type="button" variant="ghost" onclick={backToPassword}>
							{t('auth.challenge.back')}
						</Button>
					</form>
				</Card.Content>
			</Card.Root>
		{:else if showPasswordForm}
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('auth.login.title')}</Card.Title>
					<Card.Description>{t('auth.login.sub')}</Card.Description>
				</Card.Header>
				<Card.Content class="flex flex-col gap-4">
					<form class="flex flex-col gap-4" onsubmit={submitLogin}>
						<Field.Field>
							<Label for="login-email">{t('auth.login.email_label')}</Label>
							<Input
								id="login-email"
								name="email"
								type="email"
								autocomplete="username"
								required
								bind:value={email}
								aria-label={t('auth.login.email_label')}
							/>
						</Field.Field>
						<Field.Field>
							<Label for="login-password">{t('auth.login.password_label')}</Label>
							<Input
								id="login-password"
								name="password"
								type="password"
								autocomplete="current-password"
								required
								bind:value={password}
								aria-label={t('auth.login.password_label')}
							/>
						</Field.Field>
						<label class="flex items-center gap-2 text-sm">
							<input type="checkbox" bind:checked={rememberMe} />
							{t('auth.login.remember')}
						</label>
						{#if error}
							<p class="text-destructive text-sm" role="alert">{error}</p>
						{/if}
						<Button
							type="submit"
							disabled={submitting || !email.trim() || !password}
							aria-label={t('auth.login.submit')}
						>
							{submitting ? t('auth.submitting') : t('auth.login.submit')}
						</Button>
					</form>
					{#if showGoogle || showGithub}
						<div class="flex flex-col gap-2">
							<p class="text-muted-foreground text-center text-sm">{t('auth.login.or_oauth')}</p>
							{#if showGoogle}
								<Button variant="outline" href="/api/v1/auth/redirect/google">
									{t('auth.login.google')}
								</Button>
							{/if}
							{#if showGithub}
								<Button variant="outline" href="/api/v1/auth/redirect/github">
									{t('auth.login.github')}
								</Button>
							{/if}
						</div>
					{/if}
				</Card.Content>
			</Card.Root>
		{:else}
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('auth.login.title')}</Card.Title>
					<Card.Description>{t('auth.login.no_methods')}</Card.Description>
				</Card.Header>
			</Card.Root>
		{/if}
	</main>
</div>
