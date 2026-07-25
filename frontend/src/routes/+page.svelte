<script lang="ts">
	import { page } from '$app/state';
	import { t } from '$lib/i18n.svelte';
	import SketchDesk from '$lib/components/landing-krea/SketchDesk.svelte';
	import SketchLatent from '$lib/components/landing-krea/SketchLatent.svelte';
	import SketchReel from '$lib/components/landing-krea/SketchReel.svelte';
	import SketchRiver from '$lib/components/landing-krea/SketchRiver.svelte';
	import SketchSalon from '$lib/components/landing-krea/SketchSalon.svelte';
	import SketchWall from '$lib/components/landing-krea/SketchWall.svelte';
	import SketchSwitcher, {
		type SketchId
	} from '$lib/components/landing-krea/SketchSwitcher.svelte';
	import '../krea-tokens.css';

	const ids = ['wall', 'salon', 'desk', 'reel', 'latent', 'river'] as const;

	function normalise(value: string | null): SketchId {
		return (ids as readonly string[]).includes(value ?? '') ? (value as SketchId) : 'wall';
	}

	const sketch = $derived(normalise(page.url.searchParams.get('s')));
</script>

<svelte:head>
	<title>{t('hero.title1')} {t('hero.title2')} - potocolom</title>
	<meta name="description" content={t('hero.sub')} />
</svelte:head>

{#if sketch === 'salon'}
	<SketchSalon />
{:else if sketch === 'desk'}
	<SketchDesk />
{:else if sketch === 'reel'}
	<SketchReel />
{:else if sketch === 'latent'}
	<SketchLatent />
{:else if sketch === 'river'}
	<SketchRiver />
{:else}
	<SketchWall />
{/if}

<SketchSwitcher current={sketch} />
