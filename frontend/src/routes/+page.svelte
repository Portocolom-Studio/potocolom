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

<Seo titleKey="seo.home_title" descriptionKey="seo.home_description" path="/" {structuredData} />

<SketchOrbit />

<SketchSwitcher />

<ScrollToTop />
