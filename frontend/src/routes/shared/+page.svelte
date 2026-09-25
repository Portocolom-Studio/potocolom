<script lang="ts">
	import { onMount } from 'svelte';
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import { resolve } from '$app/paths';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import BrandMark from '$lib/components/brand-mark.svelte';
	import { readInviteTokenFromHash } from '$lib/auth-flow';
	import { t } from '$lib/i18n.svelte';
	import { ShareGoneError, downloadSharedPicture, resolveShare, type ShareInfo } from '$lib/share';

	const landing = PUBLIC_SITE_MODE === 'landing';

	let token = $state<string | null>(null);
	let status = $state<'loading' | 'ready' | 'invalid' | 'error'>(landing ? 'invalid' : 'loading');
	let info = $state<ShareInfo | null>(null);
	let pictureUrl = $state('');
	let pictureRetries = $state(0);

	async function load(hash: string) {
		const next = readInviteTokenFromHash(hash);
		token = next;
		info = null;
		pictureRetries = 0;
		if (!next) {
			status = 'invalid';
			return;
		}
		await resolveToken();
	}

	async function resolveToken() {
		const current = token;
		if (!current) {
			status = 'invalid';
			return;
		}
		status = 'loading';
		try {
			info = await resolveShare(current);
			pictureUrl = info.url;
			status = 'ready';
		} catch (error) {
			// Every refusal is the same 404, whether a token was never minted,
			// was revoked, or ran out, so one message names all three.
			status = error instanceof ShareGoneError ? 'invalid' : 'error';
		}
	}

	function pictureFailed() {
		// The picture address lasts a minute; resolve the token once more for
		// a fresh one before falling back to the error state.
		if (pictureRetries >= 1) {
			status = 'error';
			return;
		}
		pictureRetries += 1;
		void resolveToken();
	}

	async function download() {
		if (!token) return;
		try {
			await downloadSharedPicture(token);
		} catch (error) {
			status = error instanceof ShareGoneError ? 'invalid' : 'error';
		}
	}

	onMount(() => {
		if (landing) return;
		function onHashChange() {
			void load(location.hash);
		}
		void load(location.hash);
		window.addEventListener('hashchange', onHashChange);
		return () => window.removeEventListener('hashchange', onHashChange);
	});
</script>

<Seo
	title="Shared picture | potocolom"
	description="A picture shared from a potocolom studio."
	path="/shared"
	noindex
/>

<div class="bg-background flex min-h-dvh flex-col">
	<header class="border-b">
		<div class="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-6">
			<a class="text-base font-bold tracking-tight" href={resolve('/')}>
				<BrandMark />
			</a>
			<LanguageToggle />
		</div>
	</header>

	<main class="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center px-4 py-12 sm:px-6">
		{#if landing}
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('shared.title')}</Card.Title>
					<Card.Description>{t('shared.landing.unavailable')}</Card.Description>
				</Card.Header>
			</Card.Root>
		{:else if status === 'loading'}
			<p class="text-muted-foreground text-center text-sm">{t('shared.loading')}</p>
		{:else if status === 'ready' && info}
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('shared.title')}</Card.Title>
				</Card.Header>
				<Card.Content class="flex flex-col gap-4">
					<div
						class="overflow-hidden rounded-lg border"
						style={`aspect-ratio: ${info.asset.width} / ${info.asset.height}`}
					>
						<img
							src={pictureUrl}
							alt={info.prompt ?? t('shared.picture_alt')}
							width={info.asset.width}
							height={info.asset.height}
							class="h-full w-full object-contain"
							onerror={pictureFailed}
						/>
					</div>
					<p class="text-sm">
						<span class="text-muted-foreground">{t('shared.prompt')}: </span>
						{info.prompt ?? t('shared.no_prompt')}
					</p>
					<div class="flex items-center justify-between gap-4">
						<p class="text-muted-foreground text-sm">
							{t('shared.model')}: {info.model ?? t('shared.unknown')}
						</p>
						<Button onclick={download} variant="outline" size="sm">
							{t('shared.download')}
						</Button>
					</div>
				</Card.Content>
			</Card.Root>
		{:else if status === 'invalid'}
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('shared.title')}</Card.Title>
					<Card.Description>{t('shared.invalid')}</Card.Description>
				</Card.Header>
			</Card.Root>
		{:else}
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('shared.title')}</Card.Title>
					<Card.Description>{t('shared.error')}</Card.Description>
				</Card.Header>
			</Card.Root>
		{/if}
	</main>
</div>
