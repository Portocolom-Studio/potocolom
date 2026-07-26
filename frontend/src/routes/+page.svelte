<script lang="ts">
	import { page } from '$app/state';
	import { t } from '$lib/i18n.svelte';
	import SketchLatent from '$lib/components/landing-krea/SketchLatent.svelte';
	import SketchOrbit from '$lib/components/landing-krea/SketchOrbit.svelte';
	import SketchSwitcher, {
		type SketchId
	} from '$lib/components/landing-krea/SketchSwitcher.svelte';
	import '../krea-tokens.css';

	const ids = ['latent', 'crown', 'rings', 'disc'] as const;

	function normalise(value: string | null): SketchId {
		return (ids as readonly string[]).includes(value ?? '') ? (value as SketchId) : 'latent';
	}

	const sketch = $derived(normalise(page.url.searchParams.get('s')));
</script>

<svelte:head>
	<title>{t('hero.title1')} {t('hero.title2')} - potocolom</title>
	<meta name="description" content={t('hero.sub')} />
</svelte:head>

<!-- The three orbits are one component; only the arc geometry differs. -->
{#if sketch === 'latent'}
	<SketchLatent />
{:else}
	<SketchOrbit shape={sketch} />
{/if}

<SketchSwitcher current={sketch} />
