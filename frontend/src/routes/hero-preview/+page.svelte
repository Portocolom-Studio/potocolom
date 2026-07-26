<script lang="ts">
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import Seo from '$lib/components/Seo.svelte';

	const params = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null;
	const width = Number(params?.get('w') ?? 1200);
	const height = Number(params?.get('h') ?? 630);
	const seed = Number(params?.get('seed') ?? 42);

	function markReady() {
		(window as Window & { __heroPreviewReady?: boolean }).__heroPreviewReady = true;
	}
</script>

<Seo
	title="Hero Development Preview | potocolom"
	description="Internal hero image preview for potocolom development."
	path="/hero-preview"
	noindex
/>

<div class="frame" style:width="{width}px" style:height="{height}px">
	<LatentCanvas
		class="frame-canvas"
		{seed}
		warmupFrames={400}
		animate={false}
		onReady={markReady}
	/>
</div>

<style>
	:global(body) {
		margin: 0;
		background: #070b14;
	}

	.frame {
		overflow: hidden;
		background: #070b14;
	}

	:global(.frame-canvas) {
		display: block;
		width: 100%;
		height: 100%;
	}
</style>
