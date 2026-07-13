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
				<div class="wl-aura wl-aura-dual text-primary shrink-0">
					<Button type="submit" disabled={status === 'sending'}>
						{status === 'sending' ? t('wl.sending') : t('wl.cta')}
					</Button>
				</div>
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
				href={resolve('/privacy')}
			>
				{t('wl.privacy_note')}
			</a>
		</p>
	</section>
{/if}

<style>
	@property --wl-aura-angle {
		syntax: '<angle>';
		inherits: false;
		initial-value: 0deg;
	}

	.wl-aura {
		--aura-angle: var(--wl-aura-angle);
		--aura-padding: 0.125rem;
		--aura-radius: 9999px;
		position: relative;
		display: inline-block;
		padding: var(--aura-padding);
		border-radius: calc(var(--aura-padding) + var(--aura-radius));
		animation: wl-aura 6s linear infinite;
		background-image: conic-gradient(from var(--aura-angle), transparent 225deg, currentColor);
	}

	.wl-aura > :global(*) {
		position: relative;
		z-index: 1;
	}

	.wl-aura::before,
	.wl-aura::after {
		content: '';
		position: absolute;
		top: 50%;
		left: 50%;
		z-index: 0;
		display: block;
		width: 100%;
		height: 100%;
		border-radius: inherit;
		background-color: inherit;
		background-image: inherit;
		translate: -50% -50%;
		opacity: 0.7;
		filter: blur(0.25rem);
		animation: inherit;
	}

	.wl-aura::after {
		opacity: 0.3;
		filter: blur(1rem);
	}

	.wl-aura-dual {
		background-image: repeating-conic-gradient(
			from var(--aura-angle),
			transparent 0%,
			transparent 40%,
			currentColor 50%
		);
	}

	@keyframes wl-aura {
		to {
			--wl-aura-angle: 360deg;
			transform: translateZ(1px);
		}
	}
</style>
