<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils.js';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLElement>> = $props();
</script>

<kbd
	bind:this={ref}
	data-slot="kbd"
	class={cn(
		'border-line-strong text-dim pointer-events-none inline-flex h-4 min-w-4 items-center justify-center rounded border border-b-2 px-1 font-sans text-[11px] leading-none font-medium select-none in-data-[slot=tooltip-content]:border-background/25 in-data-[slot=tooltip-content]:text-background/70',
		className
	)}
	{...restProps}>{@render children?.()}</kbd
>
