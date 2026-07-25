<script lang="ts">
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import MoonIcon from '@lucide/svelte/icons/moon';
	import SunIcon from '@lucide/svelte/icons/sun';

	export type SketchId = 'latent' | 'reel' | 'orbit' | 'depot';

	let { current }: { current: SketchId } = $props();

	const sketches = [
		{ id: 'latent', label: 'Latent' },
		{ id: 'reel', label: 'Reel' },
		{ id: 'orbit', label: 'Orbit' },
		{ id: 'depot', label: 'Depot' }
	] as const;

	let mode = $state<'dark' | 'light'>('dark');

	function toggleMode() {
		mode = mode === 'dark' ? 'light' : 'dark';
		document.documentElement.dataset.kreaMode = mode;
	}

	function open(id: string) {
		const url = new URL(window.location.href);
		url.searchParams.set('s', id);
		void goto(`${url.pathname}${url.search}`, { keepFocus: true, replaceState: true });
	}

	function cycle(direction: -1 | 1) {
		const index = sketches.findIndex((sketch) => sketch.id === current);
		open(sketches[(index + direction + sketches.length) % sketches.length].id);
	}

	onMount(() => {
		const onKeydown = (event: KeyboardEvent) => {
			const target = event.target;
			if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) return;
			if (event.key === 'ArrowLeft') cycle(-1);
			if (event.key === 'ArrowRight') cycle(1);
		};
		window.addEventListener('keydown', onKeydown);
		return () => window.removeEventListener('keydown', onKeydown);
	});
</script>

<aside class="sketch-switcher" aria-label="Landing sketch switcher">
	{#each sketches as sketch (sketch.id)}
		<button
			type="button"
			class:on={current === sketch.id}
			onclick={() => open(sketch.id)}
			aria-pressed={current === sketch.id}
		>
			{sketch.label}
		</button>
	{/each}
	<button
		type="button"
		class="mode"
		onclick={toggleMode}
		aria-label="Switch to {mode === 'dark' ? 'light' : 'dark'} mode"
	>
		{#if mode === 'dark'}
			<SunIcon aria-hidden="true" />
		{:else}
			<MoonIcon aria-hidden="true" />
		{/if}
	</button>
</aside>

<style>
	.sketch-switcher {
		position: fixed;
		inset-block-end: 1rem;
		inset-inline-start: 50%;
		z-index: 200;
		display: flex;
		align-items: center;
		gap: 0.15rem;
		max-width: calc(100% - 1.5rem);
		overflow-x: auto;
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

	button {
		padding: 0.5rem 0.85rem;
		border: 0;
		border-radius: 999px;
		background: none;
		color: oklch(0.72 0.015 255);
		cursor: pointer;
		font: inherit;
		font-size: 0.78rem;
		font-weight: 600;
		white-space: nowrap;
	}

	button:hover {
		color: oklch(0.98 0.005 250);
	}

	button.on {
		background: oklch(0.62 0.2 255);
		color: oklch(0.98 0.005 255);
	}

	button:focus-visible {
		outline: 2px solid oklch(0.72 0.18 255);
		outline-offset: 1px;
	}

	.mode {
		display: grid;
		width: 2rem;
		height: 2rem;
		place-items: center;
		margin-inline-start: 0.2rem;
		border-inline-start: 1px solid oklch(1 0 0 / 18%);
		border-radius: 0;
	}

	.mode :global(svg) {
		width: 0.95rem;
		height: 0.95rem;
	}
</style>
