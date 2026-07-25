<script lang="ts">
	import { page } from '$app/state';
	import { t } from '$lib/i18n.svelte';
	import SketchDesk from '$lib/components/landing-krea/SketchDesk.svelte';
	import SketchLatent from '$lib/components/landing-krea/SketchLatent.svelte';
	import SketchReel from '$lib/components/landing-krea/SketchReel.svelte';
	import SketchSwitcher, {
		type SketchId
	} from '$lib/components/landing-krea/SketchSwitcher.svelte';
	import '../krea-tokens.css';

	const ids = ['latent', 'reel', 'desk'] as const;

	function normalise(value: string | null): SketchId {
		return (ids as readonly string[]).includes(value ?? '') ? (value as SketchId) : 'latent';
	}

	const sketch = $derived(normalise(page.url.searchParams.get('s')));
</script>

<svelte:head>
	<title>{t('hero.title1')} {t('hero.title2')} - potocolom</title>
	<meta name="description" content={t('hero.sub')} />
</svelte:head>

{#if sketch === 'reel'}
	<SketchReel />
{:else if sketch === 'desk'}
	<SketchDesk />
{:else}
	<SketchLatent />
{/if}

<SketchSwitcher current={sketch} />
