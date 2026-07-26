<script lang="ts">
	import { PUBLIC_SITE_MODE } from '$env/static/public';

	let {
		title,
		description,
		path,
		noindex = false,
		structuredData
	}: {
		title: string;
		description: string;
		path: string;
		noindex?: boolean;
		structuredData?: Record<string, unknown>;
	} = $props();

	const siteUrl = 'https://potocolom.leonfuller.com';
	const landing = PUBLIC_SITE_MODE === 'landing';
	const canonical = $derived(`${siteUrl}${path}`);
	const image = `${siteUrl}/og.png`;
	const robots = $derived(
		landing ? (noindex ? 'noindex, follow' : 'index, follow') : 'noindex, nofollow'
	);
	// Every less-than becomes < so a value containing a closing script tag
	// cannot end the tag it is written into. JSON parsers still see the original
	// character. The pattern uses the escape as well, since a literal less-than
	// inside a regex ends the script block for the Svelte compiler.
	const jsonLd = $derived(
		structuredData ? JSON.stringify(structuredData).replace(/\u003c/g, '\\u003c') : ''
	);
</script>

<svelte:head>
	<title>{title}</title>
	<meta name="description" content={description} />
	<meta name="robots" content={robots} />

	{#if landing && !noindex}
		<link rel="canonical" href={canonical} />
		<meta property="og:type" content="website" />
		<meta property="og:site_name" content="potocolom" />
		<meta property="og:title" content={title} />
		<meta property="og:description" content={description} />
		<meta property="og:url" content={canonical} />
		<meta property="og:image" content={image} />
		<meta property="og:image:width" content="1024" />
		<meta property="og:image:height" content="513" />
		<meta property="og:image:alt" content="potocolom realtime generative image canvas" />
		<meta name="twitter:card" content="summary_large_image" />
		<meta name="twitter:title" content={title} />
		<meta name="twitter:description" content={description} />
		<meta name="twitter:image" content={image} />
		<meta name="twitter:image:alt" content="potocolom realtime generative image canvas" />
	{/if}

	{#if landing && !noindex && jsonLd}
		{@html `<script type="application/ld+json">${jsonLd}</script>`}
	{/if}
</svelte:head>
