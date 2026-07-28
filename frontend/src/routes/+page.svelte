<script lang="ts">
	import ScrollToTop from '$lib/components/ScrollToTop.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import SketchOrbit from '$lib/components/landing/SketchOrbit.svelte';
	import SketchSwitcher from '$lib/components/landing/SketchSwitcher.svelte';
	import '../landing-tokens.css';

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

<SketchOrbit />

<SketchSwitcher />

<ScrollToTop />
