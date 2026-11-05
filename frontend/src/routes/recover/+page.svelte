<script lang="ts">
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import AuthSplit from '$lib/components/auth/AuthSplit.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import * as Card from '$lib/components/ui/card';
	import ResetFlow from '$lib/components/reset-flow.svelte';
	import { t } from '$lib/i18n.svelte';

	const landing = PUBLIC_SITE_MODE === 'landing';
</script>

<Seo
	titleKey="seo.recover_title"
	descriptionKey="seo.recover_description"
	path="/recover"
	noindex
/>

<AuthSplit fieldSide="end" heading={t('hero.title1')} headingTail={t('hero.title2')}>
	{#if landing}
		<Card.Root>
			<Card.Header>
				<h1 data-slot="card-title" class="text-base font-medium">{t('auth.recover.title')}</h1>
				<Card.Description>{t('auth.landing.unavailable')}</Card.Description>
			</Card.Header>
		</Card.Root>
	{:else}
		<ResetFlow recovery />
	{/if}
</AuthSplit>
