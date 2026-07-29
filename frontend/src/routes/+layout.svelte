<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { initializeLocale } from '$lib/i18n.svelte';

	let { children } = $props();

	onMount(initializeLocale);
</script>

<svelte:head>
	<link rel="icon" href="/favicon.ico" sizes="any" />
	<link rel="icon" href="/favicon.svg" type="image/svg+xml" />
	<link rel="apple-touch-icon" href="/apple-touch-icon.png" />
	<meta name="theme-color" content="#070b14" />
</svelte:head>

{@render children()}
