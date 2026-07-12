<script lang="ts">
	import { collageGridClass, collageImages } from '$lib/collage-images';
	import { cn } from '$lib/utils';

	let { class: className }: { class?: string } = $props();
</script>

<div
	class={cn('grid w-full grid-flow-dense grid-cols-4 gap-1.5 sm:grid-cols-6 sm:gap-2', className)}
>
	{#each collageImages as image (image.src)}
		<figure
			class={cn('border-border/60 overflow-hidden rounded-lg border', collageGridClass(image))}
			style:aspect-ratio="{image.width} / {image.height}"
		>
			<img
				src={image.src}
				alt={image.alt}
				class="block h-auto w-full"
				loading="eager"
				decoding="async"
			/>
		</figure>
	{/each}
</div>
