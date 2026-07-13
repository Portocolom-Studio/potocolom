<script lang="ts">
	import CollageGraph from '$lib/components/collage/CollageGraph.svelte';
	import CollageMasonry from '$lib/components/collage/CollageMasonry.svelte';
	import {
		collagePreviewVariants,
		masonryCollageVariants,
		type CollagePreviewVariantId
	} from '$lib/collage-variants';
	import { page } from '$app/state';

	const variantId = $derived(page.params.variant as CollagePreviewVariantId);
	const entry = $derived(collagePreviewVariants[variantId] ?? collagePreviewVariants.masonry);
	const masonryConfig = $derived(
		variantId in masonryCollageVariants
			? masonryCollageVariants[variantId as keyof typeof masonryCollageVariants]
			: null
	);
</script>

<svelte:head>
	<title>potocolom - {entry.title}</title>
</svelte:head>

<div class="bg-background min-h-svh p-4 sm:p-6">
	<header class="mb-4 flex flex-wrap items-center justify-between gap-3">
		<div>
			<p class="text-primary text-xs font-semibold tracking-[0.18em] uppercase">Collage preview</p>
			<h1 class="text-2xl font-semibold">{entry.title}</h1>
		</div>
		<a class="text-muted-foreground hover:text-foreground text-sm" href="/collage-preview"
			>Back to compare hub</a
		>
	</header>

	{#if masonryConfig}
		<CollageMasonry
			columnsClass={masonryConfig.columnsClass}
			columnGap={masonryConfig.columnGap}
			tileMargin={masonryConfig.tileMargin}
			radius={masonryConfig.radius}
		/>
	{:else}
		<CollageGraph />
	{/if}
</div>
