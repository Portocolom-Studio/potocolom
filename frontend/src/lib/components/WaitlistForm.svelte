<script lang="ts">
	// $env/static/public inlines the value at build time; the SPA fallback shell
	// never loads env.js, so the dynamic variant is always empty in this setup
	import { PUBLIC_WAITLIST_URL } from '$env/static/public';
	import { getLocale, t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
	import * as Field from '$lib/components/ui/field';
	import * as Alert from '$lib/components/ui/alert';
	import { Input } from '$lib/components/ui/input';
	import { Button } from '$lib/components/ui/button';
	import CheckIcon from '@lucide/svelte/icons/check';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';

	const endpoint = PUBLIC_WAITLIST_URL;

	let email = $state('');
	let honeypot = $state('');
	let status = $state<'idle' | 'sending' | 'done' | 'already' | 'error'>('idle');

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (honeypot !== '' || status === 'sending') return;
		status = 'sending';
		try {
			const response = await fetch(endpoint, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ email, locale: getLocale() })
			});
			const contentType = response.headers.get('content-type') ?? '';
			if (!response.ok || !contentType.includes('application/json')) {
				throw new Error(String(response.status));
			}
			const result = (await response.json()) as { status?: string };
			status = result.status === 'exists' ? 'already' : 'done';
		} catch {
			status = 'error';
		}
	}
</script>

{#if endpoint}
	<section id="waitlist" class="mx-auto w-full max-w-2xl px-4 py-24 text-center sm:px-6">
		<p class="text-primary text-xs font-semibold tracking-[0.2em] uppercase">{t('wl.kicker')}</p>
		<h2 class="mt-2 text-3xl font-semibold">{t('wl.title')}</h2>
		<p class="text-muted-foreground mx-auto mt-3 max-w-lg text-base leading-relaxed">
			{t('wl.sub')}
		</p>

		{#if status === 'done' || status === 'already'}
			<Alert.Root class="mx-auto mt-8 max-w-md text-left">
				<CheckIcon />
				<Alert.Title>{status === 'done' ? t('wl.done') : t('wl.already')}</Alert.Title>
			</Alert.Root>
		{:else}
			<form onsubmit={submit} class="mx-auto mt-8 flex w-full max-w-md items-start gap-2">
				<Field.Field class="flex-1">
					<Field.FieldLabel for="wl-email" class="sr-only">{t('wl.email_label')}</Field.FieldLabel>
					<Input
						id="wl-email"
						type="email"
						name="email"
						required
						class="h-11 rounded-full px-5"
						placeholder={t('wl.placeholder')}
						bind:value={email}
						disabled={status === 'sending'}
					/>
				</Field.Field>
				<input
					type="text"
					name="website"
					class="absolute -left-[9999px] size-px opacity-0"
					tabindex="-1"
					autocomplete="off"
					aria-hidden="true"
					bind:value={honeypot}
				/>
				<Button type="submit" disabled={status === 'sending'}>
					{status === 'sending' ? t('wl.sending') : t('wl.cta')}
				</Button>
			</form>
			{#if status === 'error'}
				<Alert.Root variant="destructive" class="mx-auto mt-4 max-w-md text-left">
					<CircleAlertIcon />
					<Alert.Title>{t('wl.error')}</Alert.Title>
				</Alert.Root>
			{/if}
		{/if}

		<p class="mt-5 text-xs">
			<a
				class="text-muted-foreground hover:text-foreground underline underline-offset-4"
				href={resolve('/privacy')}>{t('wl.privacy_note')}</a>
		</p>
	</section>
{/if}
