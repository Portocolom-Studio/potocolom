<script lang="ts">
	import { onMount } from 'svelte';
	import MoonIcon from '@lucide/svelte/icons/moon';
	import SunIcon from '@lucide/svelte/icons/sun';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { t } from '$lib/i18n.svelte';

	let mode = $state<'dark' | 'light'>('dark');

	function toggleMode() {
		mode = mode === 'dark' ? 'light' : 'dark';
		document.documentElement.dataset.landingMode = mode;
	}

	onMount(() => {
		document.documentElement.dataset.landingMode = mode;
	});
</script>

<div class="sketch-switcher">
	<LanguageToggle />
	<span class="divider" aria-hidden="true"></span>
	<button
		type="button"
		class="mode"
		onclick={toggleMode}
		aria-label={mode === 'dark' ? t('ui.switch_to_light') : t('ui.switch_to_dark')}
	>
		{#if mode === 'dark'}
			<SunIcon aria-hidden="true" />
		{:else}
			<MoonIcon aria-hidden="true" />
		{/if}
	</button>
</div>

<style>
	.sketch-switcher {
		position: fixed;
		inset-block-end: 1rem;
		inset-inline-start: 50%;
		z-index: 200;
		display: flex;
		align-items: center;
		gap: 0.15rem;
		padding: 0.3rem;
		border: 1px solid oklch(1 0 0 / 18%);
		border-radius: 999px;
		background: oklch(0.12 0.015 265 / 88%);
		color: oklch(0.98 0.005 250);
		backdrop-filter: blur(18px);
		box-shadow: 0 1rem 2.5rem oklch(0 0 0 / 45%);
		transform: translateX(-50%);
		font-family: 'Geist Variable', ui-sans-serif, system-ui, sans-serif;
	}

	.sketch-switcher :global([data-slot='toggle-group']) {
		gap: 0.15rem;
		padding: 0;
		border: 0;
	}

	.sketch-switcher :global([data-slot='toggle-group-item']) {
		height: 2.4rem;
		min-width: 2.4rem;
		padding-inline: 0.7rem;
		border: 0;
		color: oklch(0.72 0.015 255);
	}

	.sketch-switcher :global([data-slot='toggle-group-item']:hover) {
		color: oklch(0.98 0.005 250);
	}

	.sketch-switcher :global([data-slot='toggle-group-item'][data-state='on']) {
		background: oklch(0.424 0.199 265.6);
		color: oklch(0.97 0.014 254.6);
	}

	.divider {
		width: 1px;
		height: 1.4rem;
		background: oklch(1 0 0 / 14%);
	}

	.mode {
		display: grid;
		width: 2.4rem;
		height: 2.4rem;
		place-items: center;
		padding: 0;
		border: 0;
		border-radius: 999px;
		background: none;
		color: oklch(0.72 0.015 255);
		cursor: pointer;
	}

	.mode:hover {
		color: oklch(0.98 0.005 250);
	}

	.mode:focus-visible {
		outline: 2px solid oklch(0.72 0.18 255);
		outline-offset: 1px;
	}

	.mode :global(svg) {
		width: 0.95rem;
		height: 0.95rem;
	}
</style>
