<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import { METRICS_RANGE_ORDER, type MetricsRange } from '$lib/studio-metrics-range';
	import * as ToggleGroup from '$lib/components/ui/toggle-group';

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

<ToggleGroup.Root
	type="single"
	variant="outline"
	size="sm"
	spacing={0}
	class="flex w-fit"
	{value}
	onValueChange={(next) => {
		if (next) value = next as MetricsRange;
	}}
>
	{#each METRICS_RANGE_ORDER as rangeKey (rangeKey)}
		<ToggleGroup.Item value={rangeKey} class="min-w-[2.75rem] px-2.5 text-xs">
			{rangeLabels[rangeKey]}
		</ToggleGroup.Item>
	{/each}
</ToggleGroup.Root>
