<script lang="ts">
	// The page tabs a view puts in the studio top bar (Metrics, Administration).
	// Each is a button that moves the ?tab= route; a locked one keeps its
	// place with a lock and says why in its tooltip and its accessible name.
	import LockIcon from '@lucide/svelte/icons/lock';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';

	type Tab = { value: string; label: string; locked?: string | null };

	let {
		label,
		tabs,
		current,
		onSelect
	}: { label: string; tabs: Tab[]; current: string; onSelect: (value: string) => void } = $props();
</script>

<nav class="ms-4 hidden h-full items-stretch gap-0.5 md:flex" aria-label={label}>
	{#each tabs as tab (tab.value)}
		{#if tab.locked}
			<Tooltip.Root>
				<Tooltip.Trigger>
					{#snippet child({ props })}
						<button
							type="button"
							{...props}
							class="text-dim -mb-px flex cursor-not-allowed items-center gap-1.5 border-b-2 border-transparent px-2.5 font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
							aria-disabled="true"
						>
							{tab.label}
							<LockIcon class="size-3" aria-hidden="true" />
							<span class="sr-only">{tab.locked}</span>
						</button>
					{/snippet}
				</Tooltip.Trigger>
				<Tooltip.Content>{tab.locked}</Tooltip.Content>
			</Tooltip.Root>
		{:else}
			<button
				type="button"
				class={'-mb-px flex items-center border-b-2 px-2.5 font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset ' +
					(current === tab.value
						? 'border-foreground text-foreground'
						: 'text-muted-foreground hover:text-foreground border-transparent')}
				aria-current={current === tab.value ? 'page' : undefined}
				onclick={() => onSelect(tab.value)}
			>
				{tab.label}
			</button>
		{/if}
	{/each}
</nav>
