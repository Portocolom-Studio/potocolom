<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { apiFetch } from '$lib/api';
	import { createSubmitGuard, parseAuthError, readInviteTokenFromHash } from '$lib/auth-flow';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';

	const landing = PUBLIC_SITE_MODE === 'landing';
	const joinGuard = createSubmitGuard();

	let token = $state<string | null>(null);
	let ready = $state(landing);
	let password = $state('');
	let confirmPassword = $state('');
	let error = $state('');
	let submitting = $state(false);

	onMount(() => {
		if (landing) return;
		token = readInviteTokenFromHash(location.hash);
		ready = true;
	});

	async function submitJoin(event: SubmitEvent) {
		event.preventDefault();
		await joinGuard.run(async () => {
			error = '';
			if (!token) {
				error = t('auth.join.missing_token');
				return;
			}
			if (password !== confirmPassword) {
				error = t('auth.join.password_mismatch');
				return;
			}
			submitting = true;
			try {
				const response = await apiFetch('/api/v1/auth/register', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ token, password })
				});
				if (response.status === 204) {
					await goto(resolve('/app'));
					return;
				}
				const parsed = await parseAuthError(response);
				if (parsed.kind === 'policy') {
					error = t('auth.join.policy');
				} else if (parsed.kind === 'invalid') {
					error = t('auth.join.invalid');
				} else {
					error = t('auth.error.generic');
				}
			} finally {
				submitting = false;
			}
		});
	}
</script>

<Seo
	title="Join | potocolom"
	description="Accept an invitation and join potocolom."
	path="/join"
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
					<Card.Title>{t('auth.join.title')}</Card.Title>
					<Card.Description>{t('auth.landing.unavailable')}</Card.Description>
				</Card.Header>
			</Card.Root>
		{:else if !ready}
			<p class="text-muted-foreground text-center text-sm">{t('auth.loading')}</p>
		{:else if !token}
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('auth.join.title')}</Card.Title>
					<Card.Description>{t('auth.join.missing_token')}</Card.Description>
				</Card.Header>
			</Card.Root>
		{:else}
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('auth.join.title')}</Card.Title>
					<Card.Description>{t('auth.join.sub')}</Card.Description>
				</Card.Header>
				<Card.Content>
					<form class="flex flex-col gap-4" onsubmit={submitJoin}>
						<Field.Field>
							<Label for="join-password">{t('auth.join.password_label')}</Label>
							<Input
								id="join-password"
								name="password"
								type="password"
								autocomplete="new-password"
								required
								bind:value={password}
								aria-label={t('auth.join.password_label')}
							/>
						</Field.Field>
						<Field.Field>
							<Label for="join-confirm">{t('auth.join.confirm_label')}</Label>
							<Input
								id="join-confirm"
								name="confirm"
								type="password"
								autocomplete="new-password"
								required
								bind:value={confirmPassword}
								aria-label={t('auth.join.confirm_label')}
							/>
						</Field.Field>
						{#if error}
							<p class="text-destructive text-sm" role="alert">{error}</p>
						{/if}
						<Button
							type="submit"
							disabled={submitting || !password || !confirmPassword}
							aria-label={t('auth.join.submit')}
						>
							{submitting ? t('auth.submitting') : t('auth.join.submit')}
						</Button>
					</form>
				</Card.Content>
			</Card.Root>
		{/if}
	</main>
</div>
