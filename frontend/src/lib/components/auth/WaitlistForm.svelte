<script lang="ts">
	// The cloud waitlist signup, shown as the second tab of /login when
	// PUBLIC_WAITLIST_URL is baked in at build time.
	import { resolve } from '$app/paths';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { getLocale, t } from '$lib/i18n.svelte';
	import { joinWaitlist } from '$lib/waitlist';

	let { endpoint }: { endpoint: string } = $props();

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';

	let email = $state('');
	let honeypot = $state('');
	let status = $state<'idle' | 'sending' | 'done' | 'already' | 'error'>('idle');

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (!endpoint || honeypot !== '' || status === 'sending') return;
		status = 'sending';
		status = await joinWaitlist(endpoint, email, getLocale());
	}
</script>

<Card.Root>
	<Card.Header>
		<h1 data-slot="card-title" class="text-base font-medium">{t('wl.title')}</h1>
		<Card.Description>{t('wl.sub')}</Card.Description>
	</Card.Header>
	<Card.Content class="flex flex-col gap-4">
		<div role="status">
			{#if status === 'done' || status === 'already'}
				<p class="text-sm">{status === 'done' ? t('wl.done') : t('wl.already')}</p>
			{/if}
		</div>
		{#if status !== 'done' && status !== 'already'}
			<form class="flex flex-col gap-4" onsubmit={submit}>
				<Field.Field>
					<Label for="waitlist-email">{t('wl.email_label')}</Label>
					<Input
						id="waitlist-email"
						name="email"
						type="email"
						autocomplete="email"
						spellcheck={false}
						required
						placeholder={t('wl.placeholder')}
						bind:value={email}
						disabled={status === 'sending'}
					/>
				</Field.Field>
				<input
					type="text"
					name="website"
					class="waitlist-honey"
					tabindex="-1"
					autocomplete="off"
					aria-hidden="true"
					bind:value={honeypot}
				/>
				{#if status === 'error'}
					<p class="text-destructive text-sm" role="alert">{t('wl.error')}</p>
				{/if}
				<Button type="submit" disabled={status === 'sending'}>
					{status === 'sending' ? t('wl.sending') : t('wl.cta')}
				</Button>
			</form>
		{/if}
		<p class="text-muted-foreground text-sm">
			<a href={resolve('/privacy')}>{t('wl.privacy_note')}</a>
		</p>
		<p class="text-muted-foreground text-sm">
			{t('wl.selfhost_note')}
			<a href={repoUrl}>{t('wl.selfhost_link')}</a>.
		</p>
	</Card.Content>
</Card.Root>

<style>
	.waitlist-honey {
		position: absolute;
		left: -9999px;
		width: 1px;
		height: 1px;
		opacity: 0;
	}
</style>
