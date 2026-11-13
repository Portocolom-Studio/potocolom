<script lang="ts">
	import LandingPricing from '$lib/components/landing/LandingPricing.svelte';
	import LatentShell from '$lib/components/landing/LatentShell.svelte';
	import ScrollToTop from '$lib/components/ScrollToTop.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import { t } from '$lib/i18n.svelte';
	import '../../../landing-tokens.css';
</script>

<Seo titleKey="pricing.page_title" descriptionKey="pricing.page_description" path="/pricing" />

<LatentShell>
	<main id="main">
		<h1 class="visually-hidden">{t('nav.pricing')}</h1>
		<LandingPricing full />
	</main>
</LatentShell>

<ScrollToTop />

<style>
	.visually-hidden {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		padding: 0;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
</style>
