<script lang="ts">
	import {
		collageImages,
		collageLandingSources,
		collageLandingTileClass,
		collageTileClass
	} from '$lib/collage-images';
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
		eagerCount = 0,
		loading = 'lazy' as 'lazy' | 'eager'
	}: {
		class?: string;
		weighted?: boolean;
		landing?: boolean;
		eagerCount?: number;
		loading?: 'lazy' | 'eager';
		columnCount?: number;
		columnWidth?: number;
	} & Partial<MasonryCollageConfig> = $props();

	function tileLoading(index: number): 'lazy' | 'eager' {
		if (landing && index < eagerCount) return 'eager';
		if (landing) return 'lazy';
		return loading;
	}

	function markLoaded(event: Event) {
		const img = event.currentTarget;
		if (img instanceof HTMLImageElement) img.dataset.loaded = 'true';
	}

	function primeLoaded(img: HTMLImageElement) {
		if (img.complete && img.naturalWidth > 0) img.dataset.loaded = 'true';
	}
</script>

<div
	class={cn(columnCount || columnWidth ? undefined : columnsClass, columnGap, className)}
	style:column-count={columnCount}
	style:column-width={columnWidth ? `${columnWidth}px` : undefined}
>
	{#each collageImages as image, index (image.src)}
		{@const landingSources = landing ? collageLandingSources(image) : null}
		<figure
			class={cn(
				'border-border/60 break-inside-avoid overflow-hidden border',
				tileMargin,
				radius,
				weighted && collageTileClass(image),
				landing && collageLandingTileClass(image)
			)}
			style:aspect-ratio="{image.width} / {image.height}"
		>
			{#if landing && landingSources}
				<img
					src={landingSources.src}
					srcset={landingSources.srcset}
					sizes="(max-width: 640px) 170px, 235px"
					alt={image.alt}
					class="gallery-tile-img block h-auto w-full"
					loading={tileLoading(index)}
					decoding="async"
					onload={markLoaded}
					use:primeLoaded
				/>
			{:else}
				<img
					src={image.src}
					alt={image.alt}
					class="block h-auto w-full"
					loading={tileLoading(index)}
					decoding="async"
				/>
			{/if}
		</figure>
	{/each}
</div>

<style>
	:global(.gallery-tile-img) {
		opacity: 0;
		transition: opacity 300ms ease;
	}

	:global(.gallery-tile-img[data-loaded='true']) {
		opacity: 1;
	}
</style>
