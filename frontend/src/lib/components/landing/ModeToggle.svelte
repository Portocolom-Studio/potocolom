<script lang="ts">
	import { onMount } from 'svelte';
	import MoonIcon from '@lucide/svelte/icons/moon';
	import SunIcon from '@lucide/svelte/icons/sun';
	import { applyLandingMode, readLandingMode, type LandingMode } from '$lib/landing-mode';
	import { t } from '$lib/i18n.svelte';

	let mode = $state<LandingMode>('dark');

	function toggleMode() {
		mode = mode === 'dark' ? 'light' : 'dark';
		applyLandingMode(mode);
	}

	onMount(() => {
		mode = readLandingMode();
		applyLandingMode(mode, false);
	});
</script>

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

<style>
	.mode {
		display: grid;
		width: 2.4rem;
		height: 2.4rem;
		place-items: center;
		padding: 0;
		border: 0;
		border-radius: 999px;
		background: none;
		color: var(--mode-toggle-color, var(--k-muted));
		cursor: pointer;
	}

	.mode:hover {
		color: var(--mode-toggle-hover, var(--k-ink));
	}

	.mode:focus-visible {
		outline: 2px solid var(--mode-toggle-focus, var(--k-accent));
		outline-offset: 1px;
	}

	.mode :global(svg) {
		width: 0.95rem;
		height: 0.95rem;
	}
</style>
