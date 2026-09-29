<script lang="ts" module>
	import { getContext, setContext } from 'svelte';
	import type { VariantProps } from 'tailwind-variants';
	import { toggleVariants } from '$lib/components/ui/toggle/index.js';

	type ToggleVariants = VariantProps<typeof toggleVariants>;

	interface ToggleGroupContext extends ToggleVariants {
		spacing?: number;
		orientation?: 'horizontal' | 'vertical';
	}

	export function setToggleGroupCtx(props: ToggleGroupContext) {
		setContext('toggleGroup', props);
	}

	export function getToggleGroupCtx() {
		return getContext<Required<ToggleGroupContext>>('toggleGroup');
	}
</script>

<script lang="ts">
	import { ToggleGroup as ToggleGroupPrimitive } from 'bits-ui';
	import { cn } from '$lib/utils.js';
	import { acceptsToggleValue } from '$lib/toggle-group-value';

	let {
		ref = $bindable(null),
		value = $bindable(),
		class: className,
		size = 'default',
		spacing = 0,
		orientation = 'horizontal',
		variant = 'default',
		onValueChange,
		children,
		...restProps
	}: ToggleGroupPrimitive.RootProps &
		ToggleVariants & {
			spacing?: number;
			orientation?: 'horizontal' | 'vertical';
		} = $props();

	setToggleGroupCtx({
		get variant() {
			return variant;
		},
		get size() {
			return size;
		},
		get spacing() {
			return spacing;
		},
		get orientation() {
			return orientation;
		}
	});
</script>

<!--
Discriminated Unions + Destructing (required for bindable) do not
get along, so we shut typescript up by casting `value` to `never`.
-->
<ToggleGroupPrimitive.Root
	bind:value={
		() => value as never,
		(next: never) => {
			if (acceptsToggleValue(restProps.type, next)) value = next;
		}
	}
	onValueChange={((next: never) => {
		if (acceptsToggleValue(restProps.type, next)) onValueChange?.(next);
	}) as never}
	bind:ref
	{orientation}
	data-slot="toggle-group"
	data-variant={variant}
	data-size={size}
	data-spacing={spacing}
	style={`--gap: ${spacing}`}
	class={cn(
		'data-[spacing=0]:data-[variant=outline]:rounded-4xl group/toggle-group flex w-fit flex-row items-center gap-[--spacing(var(--gap))] data-vertical:flex-col data-vertical:items-stretch',
		className
	)}
	{...restProps}
>
	{#snippet child({ props })}
		<!-- bits-ui gives the root role="group" whatever it holds, but single
		mode renders role="radio" items, which only a radiogroup owns. -->
		<div {...props} role={restProps.type === 'single' ? 'radiogroup' : 'group'}>
			{@render children?.()}
		</div>
	{/snippet}
</ToggleGroupPrimitive.Root>
