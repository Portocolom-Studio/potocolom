<script lang="ts">
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import * as Card from '$lib/components/ui/card';
	import ResetFlow from '$lib/components/reset-flow.svelte';
	import BrandMark from '$lib/components/brand-mark.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';

	const landing = PUBLIC_SITE_MODE === 'landing';
</script>

<Seo titleKey="seo.reset_title" descriptionKey="seo.reset_description" path="/reset" noindex />

<div class="bg-background flex min-h-dvh flex-col">
	<header class="border-b">
		<div class="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-6">
			<a class="text-base font-bold tracking-tight" href={resolve('/')}>
				<BrandMark />
			</a>
			<LanguageToggle />
		</div>
	</header>

	<main class="mx-auto flex w-full max-w-md flex-1 flex-col justify-center px-4 py-12 sm:px-6">
		{#if landing}
			<Card.Root>
				<Card.Header>
					<h1 data-slot="card-title" class="text-base font-medium">{t('auth.reset.title')}</h1>
					<Card.Description>{t('auth.landing.unavailable')}</Card.Description>
				</Card.Header>
			</Card.Root>
		{:else}
			<ResetFlow />
		{/if}
	</main>
</div>
