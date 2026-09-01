<script lang="ts">
	import { Slider as SliderPrimitive } from 'bits-ui';
	import { cn, type WithoutChildrenOrChild } from '$lib/utils.js';

	let {
		ref = $bindable(null),
		value = $bindable(),
		orientation = 'horizontal',
		class: className,
		// What a screen reader should announce instead of the raw position.
		// The thumb carries role="slider", so this cannot ride on the root:
		// a track measured in parameter steps would otherwise be read out as
		// "4 of 6" for a value of 6 (issue #250).
		valueText,
		// role="slider", the tabindex and the value all live on the thumb, so an
		// id or an aria-labelledby put on the root addresses a plain span and
		// names nothing. bits-ui has no label part; it forwards span attributes
		// to the thumb, which is where both have to land. Single-thumb only:
		// several thumbs would share the one id.
		thumbId,
		labelledBy,
		...restProps
	}: WithoutChildrenOrChild<SliderPrimitive.RootProps> & {
		valueText?: string;
		thumbId?: string;
		labelledBy?: string;
	} = $props();
</script>

<!--
Discriminated Unions + Destructing (required for bindable) do not
get along, so we shut typescript up by casting `value` to `never`.
-->
<SliderPrimitive.Root
	bind:ref
	bind:value={value as never}
	data-slot="slider"
	{orientation}
	class={cn(
		'data-vertical:min-h-40 relative flex w-full touch-none items-center select-none data-disabled:opacity-50 data-vertical:h-full data-vertical:w-auto data-vertical:flex-col',
		className
	)}
	{...restProps}
>
	{#snippet children({ thumbItems })}
		<span
			data-slot="slider-track"
			data-orientation={orientation}
			class={cn(
				'bg-muted rounded-4xl data-horizontal:h-3 data-horizontal:w-full data-vertical:h-full data-vertical:w-3 bg-muted relative grow overflow-hidden data-horizontal:w-full data-vertical:h-full'
			)}
		>
			<SliderPrimitive.Range
				data-slot="slider-range"
				class={cn('bg-primary absolute select-none data-horizontal:h-full data-vertical:w-full')}
			/>
		</span>
		{#each thumbItems as thumb (thumb.index)}
			<SliderPrimitive.Thumb
				data-slot="slider-thumb"
				index={thumb.index}
				id={thumbId}
				aria-labelledby={labelledBy}
				aria-valuetext={valueText}
				class="border-primary ring-ring/50 size-4 rounded-4xl border bg-white shadow-sm transition-colors hover:ring-4 focus-visible:ring-4 focus-visible:outline-hidden block shrink-0 select-none disabled:pointer-events-none disabled:opacity-50"
			/>
		{/each}
	{/snippet}
</SliderPrimitive.Root>
