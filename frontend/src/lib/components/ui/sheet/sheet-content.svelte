<script lang="ts" module>
	export type Side = 'top' | 'right' | 'bottom' | 'left';
</script>

<script lang="ts">
	// Enter and exit are CSS transitions keyed on bits-ui's data-starting-style
	// and data-ending-style, not one-shot keyframes: closing a sheet halfway
	// through its entrance reverses from where it is, and a swipe can hand
	// its position straight to the closing transition.
	import { Dialog as SheetPrimitive } from 'bits-ui';
	import type { Snippet } from 'svelte';
	import SheetPortal from './sheet-portal.svelte';
	import SheetOverlay from './sheet-overlay.svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import XIcon from '@lucide/svelte/icons/x';
	import { cn, type WithoutChildrenOrChild } from '$lib/utils.js';
	import type { ComponentProps } from 'svelte';

	let {
		ref = $bindable(null),
		class: className,
		side = 'right',
		showCloseButton = true,
		portalProps,
		children,
		...restProps
	}: WithoutChildrenOrChild<SheetPrimitive.ContentProps> & {
		portalProps?: WithoutChildrenOrChild<ComponentProps<typeof SheetPortal>>;
		side?: Side;
		showCloseButton?: boolean;
		children: Snippet;
	} = $props();
</script>

<SheetPortal {...portalProps}>
	<SheetOverlay />
	<SheetPrimitive.Content
		bind:ref
		data-slot="sheet-content"
		data-side={side}
		class={cn(
			'bg-popover text-popover-foreground fixed z-50 flex flex-col bg-clip-padding text-sm shadow-lg transition-[translate] duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:transition-none data-[side=bottom]:inset-x-0 data-[side=bottom]:bottom-0 data-[side=bottom]:h-auto data-[side=bottom]:border-t data-[side=left]:inset-y-0 data-[side=left]:left-0 data-[side=left]:h-full data-[side=left]:w-3/4 data-[side=left]:border-r data-[side=right]:inset-y-0 data-[side=right]:right-0 data-[side=right]:h-full data-[side=right]:w-3/4 data-[side=right]:border-l data-[side=top]:inset-x-0 data-[side=top]:top-0 data-[side=top]:h-auto data-[side=top]:border-b data-[side=left]:sm:max-w-sm data-[side=right]:sm:max-w-sm data-[side=bottom]:data-starting-style:translate-y-full data-[side=bottom]:data-ending-style:translate-y-full data-[side=left]:data-starting-style:-translate-x-full data-[side=left]:data-ending-style:-translate-x-full data-[side=right]:data-starting-style:translate-x-full data-[side=right]:data-ending-style:translate-x-full data-[side=top]:data-starting-style:-translate-y-full data-[side=top]:data-ending-style:-translate-y-full',
			className
		)}
		{...restProps}
	>
		{@render children?.()}
		{#if showCloseButton}
			<SheetPrimitive.Close data-slot="sheet-close">
				{#snippet child({ props })}
					<Button variant="ghost" class="hit-slop absolute top-4 right-4" size="icon-sm" {...props}>
						<XIcon />
						<span class="sr-only">Close</span>
					</Button>
				{/snippet}
			</SheetPrimitive.Close>
		{/if}
	</SheetPrimitive.Content>
</SheetPortal>
