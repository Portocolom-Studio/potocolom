<script lang="ts">
	import {
		attachLatentCanvas,
		type LatentCanvasApi,
		type LatentCanvasOptions
	} from '$lib/latent-canvas-scene';
	import { cn } from '$lib/utils';

	let {
		class: className,
		seed,
		warmupFrames,
		animate,
		followCursor,
		onAttach,
		onReady
	}: {
		class?: string;
		seed?: number;
		warmupFrames?: number;
		animate?: boolean;
		followCursor?: boolean;
		onAttach?: (api: LatentCanvasApi) => void;
		onReady?: () => void;
	} = $props();

	function scene(canvas: HTMLCanvasElement) {
		const options: LatentCanvasOptions = {
			seed,
			warmupFrames,
			animate,
			followCursor,
			onAttach,
			onReady
		};
		return attachLatentCanvas(canvas, options);
	}
</script>

<canvas class={cn('block size-full', className)} {@attach scene} aria-hidden="true"></canvas>
