<script lang="ts">
	import { collageImage } from '$lib/collage-images';
	import { cn } from '$lib/utils';

	let { class: className }: { class?: string } = $props();

	const nodes = [
		{ x: 50, y: 30, size: 'lg', image: collageImage('squirrel.jpg') },
		{ x: 38, y: 50, size: 'md', image: collageImage('fox.png') },
		{ x: 62, y: 48, size: 'md', image: collageImage('cat.jpg') },
		{ x: 44, y: 66, size: 'sm', image: collageImage('abstract_cat.png') },
		{ x: 56, y: 64, size: 'sm', image: collageImage('output.png') },
		{ x: 50, y: 52, size: 'md', image: collageImage('jqbLhzV7.jpeg') },
		{ x: 40, y: 36, size: 'sm', image: collageImage('flow.jpg') },
		{ x: 60, y: 38, size: 'sm', image: collageImage('mountain_chibbi.png') },
		{ x: 48, y: 42, size: 'sm', image: collageImage('abstract_man.jpg') },
		{ x: 54, y: 58, size: 'sm', image: collageImage('41UpghU7P1L-3126336320.jpg') }
	] as const;

	const edges = [
		[0, 5],
		[0, 1],
		[0, 2],
		[1, 5],
		[2, 5],
		[1, 3],
		[2, 4],
		[3, 5],
		[4, 5],
		[0, 6],
		[0, 7],
		[5, 8],
		[5, 9],
		[1, 8],
		[2, 9]
	] as const;

	const sizes = {
		sm: 'h-16 w-16 sm:h-20 sm:w-20',
		md: 'h-24 w-24 sm:h-28 sm:w-28',
		lg: 'h-32 w-32 sm:h-36 sm:w-36'
	} as const;
</script>

<div
	class={cn('border-border/60 relative min-h-[28rem] overflow-hidden rounded-xl border', className)}
>
	<svg
		class="text-primary/35 absolute inset-0 size-full"
		viewBox="0 0 100 100"
		preserveAspectRatio="xMidYMid meet"
	>
		{#each edges as [from, to], index (index)}
			<line
				x1={nodes[from].x}
				y1={nodes[from].y}
				x2={nodes[to].x}
				y2={nodes[to].y}
				stroke="currentColor"
				stroke-width="0.3"
			/>
		{/each}
	</svg>

	{#each nodes as node (node.image.src)}
		<figure
			class={cn(
				'border-border/60 absolute -translate-x-1/2 -translate-y-1/2 overflow-hidden rounded-full border-2 shadow-lg',
				sizes[node.size]
			)}
			style={`left:${node.x}%;top:${node.y}%`}
		>
			<img
				src={node.image.src}
				alt={node.image.alt}
				class="size-full object-cover"
				loading="lazy"
			/>
		</figure>
	{/each}
</div>
