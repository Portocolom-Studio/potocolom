<script lang="ts">
	import { collageImages, collageLandingTileClass, collageTileClass } from '$lib/collage-images';
	import type { MasonryCollageConfig } from '$lib/collage-variants';
	import { cn } from '$lib/utils';

	let {
		class: className,
		columnsClass = 'columns-2 sm:columns-3 lg:columns-4',
		columnCount,
		columnWidth,
		columnGap = 'gap-3',
		tileMargin = 'mb-3',
		radius = 'rounded-xl',
		weighted = false,
		landing = false,
		loading = 'lazy' as 'lazy' | 'eager'
	}: {
		class?: string;
		weighted?: boolean;
		landing?: boolean;
		loading?: 'lazy' | 'eager';
		columnCount?: number;
		columnWidth?: number;
	} & Partial<MasonryCollageConfig> = $props();
</script>

<div
	class={cn(columnCount || columnWidth ? undefined : columnsClass, columnGap, className)}
	style:column-count={columnCount}
	style:column-width={columnWidth ? `${columnWidth}px` : undefined}
>
	{#each collageImages as image (image.src)}
		<figure
			class={cn(
				'border-border/60 break-inside-avoid overflow-hidden border',
				tileMargin,
				radius,
				weighted && collageTileClass(image),
				landing && collageLandingTileClass(image)
			)}
		>
			<img src={image.src} alt={image.alt} class="block h-auto w-full" {loading} decoding="async" />
		</figure>
	{/each}
</div>
