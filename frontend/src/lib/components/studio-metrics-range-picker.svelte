<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import {
		isMetricsRangeEnabled,
		METRICS_RANGE_ORDER,
		type MetricsRange
	} from '$lib/studio-metrics-range';
	import * as ToggleGroup from '$lib/components/ui/toggle-group';
	import * as Tooltip from '$lib/components/ui/tooltip';

	let {
		value = $bindable<MetricsRange>('5m')
	}: {
		value?: MetricsRange;
	} = $props();

	const rangeLabels: Record<MetricsRange, string> = {
		'5m': t('app.metrics.range_5m'),
		'1h': t('app.metrics.range_1h'),
		'24h': t('app.metrics.range_24h'),
		'7d': t('app.metrics.range_7d'),
		'30d': t('app.metrics.range_30d')
	};
</script>

<Tooltip.Provider>
	<ToggleGroup.Root
		type="single"
		variant="outline"
		size="sm"
		spacing={0}
		class="flex w-fit"
		{value}
		onValueChange={(next) => {
			if (next && isMetricsRangeEnabled(next as MetricsRange)) {
				value = next as MetricsRange;
			}
		}}
	>
		{#each METRICS_RANGE_ORDER as rangeKey (rangeKey)}
			{@const enabled = isMetricsRangeEnabled(rangeKey)}
			{#if enabled}
				<ToggleGroup.Item value={rangeKey} class="min-w-[2.75rem] px-2.5 text-xs">
					{rangeLabels[rangeKey]}
				</ToggleGroup.Item>
			{:else}
				<Tooltip.Root>
					<Tooltip.Trigger>
						{#snippet child({ props })}
							<ToggleGroup.Item
								{...props}
								value={rangeKey}
								disabled
								class="min-w-[2.75rem] px-2.5 text-xs"
							>
								{rangeLabels[rangeKey]}
							</ToggleGroup.Item>
						{/snippet}
					</Tooltip.Trigger>
					<Tooltip.Content>{t('app.metrics.range_requires_persistence')}</Tooltip.Content>
				</Tooltip.Root>
			{/if}
		{/each}
	</ToggleGroup.Root>
</Tooltip.Provider>
