<script lang="ts">
	import { Select as SelectPrimitive } from 'bits-ui';
	import { cn, type WithoutChild } from '$lib/utils.js';
	import ChevronDownIcon from '@lucide/svelte/icons/chevron-down';

	let {
		ref = $bindable(null),
		class: className,
		children,
		size = 'default',
		variant = 'default',
		...restProps
	}: WithoutChild<SelectPrimitive.TriggerProps> & {
		size?: 'xs' | 'sm' | 'default';
		variant?: 'default' | 'chip';
	} = $props();
</script>

<SelectPrimitive.Trigger
	bind:ref
	data-slot="select-trigger"
	data-size={size}
	class={cn(
		"border-input data-placeholder:text-muted-foreground bg-input/30 dark:hover:bg-input/50 focus-visible:border-ring focus-visible:ring-ring/50 aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive dark:aria-invalid:border-destructive/50 gap-1.5 rounded-4xl border px-3 py-2 font-sans text-sm transition-colors focus-visible:ring-[3px] aria-invalid:ring-[3px] data-[size=default]:h-9 data-[size=sm]:h-8 data-[size=xs]:h-[26px] *:data-[slot=select-value]:flex *:data-[slot=select-value]:gap-1.5 [&_svg:not([class*='size-'])]:size-4 flex w-fit items-center justify-between whitespace-nowrap outline-none disabled:cursor-not-allowed disabled:opacity-50 *:data-[slot=select-value]:line-clamp-1 *:data-[slot=select-value]:flex *:data-[slot=select-value]:items-center [&_svg]:pointer-events-none [&_svg]:shrink-0",
		variant === 'chip' &&
			'border-line-strong bg-transparent text-muted-foreground hover:bg-hover dark:hover:bg-hover hover:text-foreground data-[active=true]:bg-hover data-[active=true]:text-foreground data-placeholder:text-muted-foreground rounded-full px-2.5 py-0 text-xs font-medium focus-visible:ring-ring/60 focus-visible:ring-2',
		className
	)}
	{...restProps}
>
	{@render children?.()}
	<ChevronDownIcon
		class="text-muted-foreground pointer-events-none {size === 'xs' ? 'size-3.5' : 'size-4'}"
	/>
</SelectPrimitive.Trigger>
