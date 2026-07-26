<script lang="ts">
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import Seo from '$lib/components/Seo.svelte';
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

	// Prerender (from main) forbids reading searchParams at build time; default
	// to latent on the server and resolve ?s= after hydrate.
	const sketch = $derived(
		browser ? normalise(page.url.searchParams.get('s')) : ('latent' as SketchId)
	);

	const structuredData = {
		'@context': 'https://schema.org',
		'@graph': [
			{
				'@type': 'WebSite',
				name: 'potocolom',
				url: 'https://potocolom.leonfuller.com/',
				description: 'Pre-alpha open source platform for realtime generative image workflows.',
				inLanguage: 'en'
			},
			{
				'@type': 'SoftwareSourceCode',
				name: 'potocolom',
				description: 'AGPL-3.0 source code for a self-hostable realtime generative image platform.',
				codeRepository: 'https://github.com/portocolom-studio/potocolom',
				license: 'https://www.gnu.org/licenses/agpl-3.0.html',
				programmingLanguage: ['TypeScript', 'Python'],
				runtimePlatform: ['Web', 'Linux'],
				isAccessibleForFree: true
			}
		]
	};
</script>

<Seo
	title="potocolom | Open Source Realtime AI Image Generation"
	description="Sketch on a canvas and watch a diffusion model render live. potocolom is a pre-alpha AGPL-3.0 platform you can self-host for free."
	path="/"
	{structuredData}
/>

<!-- The three orbits are one component; only the arc geometry differs. -->
{#if sketch === 'latent'}
	<SketchLatent />
{:else}
	<SketchOrbit shape={sketch} />
{/if}

<SketchSwitcher current={sketch} />
