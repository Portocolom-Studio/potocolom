<script lang="ts">
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import ModeToggle from './ModeToggle.svelte';
</script>

<div class="sketch-switcher">
	<LanguageToggle />
	<span class="divider" aria-hidden="true"></span>
	<ModeToggle />
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
		font-family: 'Lato', ui-sans-serif, system-ui, sans-serif;
		--mode-toggle-color: oklch(0.72 0.015 255);
		--mode-toggle-hover: oklch(0.98 0.005 250);
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
</style>
