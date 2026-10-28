<script lang="ts">
	import XIcon from '@lucide/svelte/icons/x';
	import * as Select from '$lib/components/ui/select';
	import * as Field from '$lib/components/ui/field';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { t } from '$lib/i18n.svelte';
	import {
		clearImageFilters,
		customRangeReversed,
		imageSizeBins,
		imageFiltersActive,
		modelOptionLabel,
		type ImageFilters,
		type ImageModelOption,
		type ImageRangePreset,
		type ImageSizeBin
	} from '$lib/image-filters';

	let {
		filters,
		options = [],
		loading = false,
		failed = false,
		onchange
	}: {
		filters: ImageFilters;
		options?: ImageModelOption[];
		loading?: boolean;
		failed?: boolean;
		onchange?: (next: ImageFilters) => void;
	} = $props();
	let selectedModels = $state<string[]>([]);
	let selectedSizes = $state<ImageSizeBin[]>([]);

	// An empty selection represents All models, so it never enters the value.
	const ranges: { value: ImageRangePreset; label: string }[] = $derived([
		{ value: 'all', label: t('app.images.range_all') },
		{ value: 'today', label: t('app.images.range_today') },
		{ value: 'last7', label: t('app.images.range_last7') },
		{ value: 'last30', label: t('app.images.range_last30') },
		{ value: 'custom', label: t('app.images.range_custom') }
	]);

	const modelTriggerLabel = $derived.by(() => {
		if (loading) return t('app.images.filter_models_loading');
		if (failed) return t('app.images.filter_models_failed');
		if (filters.models.length === 0) return t('app.images.filter_all_models');
		if (filters.models.length === 1) {
			const only = filters.models[0];
			return modelOptionLabel(options.find((option) => option.id === only)) || only;
		}
		return t('app.images.filter_models_count').replace('{count}', String(filters.models.length));
	});
	const sizeLabels: Record<ImageSizeBin, string> = $derived({
		'512': t('app.images.size_512'),
		'768': t('app.images.size_768'),
		'1k': t('app.images.size_1k'),
		'2k': t('app.images.size_2k'),
		'4k': t('app.images.size_4k'),
		above_4k: t('app.images.size_above_4k'),
		unknown: t('app.images.size_unknown')
	});
	const sizeOptions = $derived(
		imageSizeBins.map((bin) => ({
			value: bin.value,
			label:
				bin.value === 'unknown'
					? `${sizeLabels[bin.value]} (${t('app.images.size_unknown_detail')})`
					: `${sizeLabels[bin.value]} (${bin.max === null ? `${bin.min}+` : `${bin.min}-${bin.max - 1}`} px)`,
			title:
				bin.value === 'unknown'
					? t('app.images.size_unknown_detail')
					: `${bin.max === null ? `${bin.min}+` : `${bin.min}-${bin.max - 1}`} px`
		}))
	);
	const sizeTriggerLabel = $derived.by(() => {
		if (filters.sizes.length === 0) return t('app.images.filter_all_sizes');
		if (filters.sizes.length === 1) return sizeLabels[filters.sizes[0]];
		return t('app.images.filter_sizes_count').replace('{count}', String(filters.sizes.length));
	});

	const rangeInvalid = $derived(customRangeReversed(filters));
	const rangeIncomplete = $derived(
		filters.range === 'custom' && (filters.from === '' || filters.through === '')
	);
	const customFromId = 'image-filter-from';
	const customThroughId = 'image-filter-through';

	function onModelsChange(next: string[] | undefined): void {
		const models = next ?? [];
		selectedModels = [...models];
		onchange?.({ ...filters, models });
	}

	function onSizesChange(next: string[] | undefined): void {
		const sizes = (next ?? []).filter((size): size is ImageSizeBin =>
			imageSizeBins.some((bin) => bin.value === size)
		);
		selectedSizes = [...sizes];
		onchange?.({ ...filters, sizes });
	}

	function onRangeChange(next: string | undefined): void {
		if (!next) return;
		if (
			next === 'all' ||
			next === 'today' ||
			next === 'last7' ||
			next === 'last30' ||
			next === 'custom'
		) {
			onchange?.({ ...filters, range: next });
		}
	}

	function setDay(bound: 'from' | 'through', day: string): void {
		onchange?.({ ...filters, [bound]: day });
	}

	$effect(() => {
		const incoming = filters.models;
		if (
			incoming.length !== selectedModels.length ||
			incoming.some((id, index) => id !== selectedModels[index])
		) {
			selectedModels = [...incoming];
		}
		if (
			filters.sizes.length !== selectedSizes.length ||
			filters.sizes.some((size, index) => size !== selectedSizes[index])
		) {
			selectedSizes = [...filters.sizes];
		}
	});
</script>

<div class="flex flex-wrap items-center gap-1.5">
	<Select.Root
		type="multiple"
		value={selectedModels}
		items={options.map((option) => ({ value: option.id, label: modelOptionLabel(option) }))}
		onValueChange={onModelsChange}
	>
		<Select.Trigger
			size="xs"
			variant="chip"
			data-active={filters.models.length > 0}
			aria-label={t('app.images.filter_model')}
			disabled={loading || failed}
		>
			{modelTriggerLabel}
		</Select.Trigger>
		<Select.Content class="max-h-72">
			<Select.Group>
				{#each options as option (option.id)}
					<Select.Item value={option.id} label={modelOptionLabel(option)} />
				{/each}
			</Select.Group>
		</Select.Content>
	</Select.Root>

	<Select.Root
		type="multiple"
		value={selectedSizes}
		items={sizeOptions}
		onValueChange={onSizesChange}
	>
		<Select.Trigger
			size="xs"
			variant="chip"
			data-active={filters.sizes.length > 0}
			aria-label={t('app.images.filter_size')}
		>
			{sizeTriggerLabel}
		</Select.Trigger>
		<Select.Content class="max-h-72">
			<Select.Group>
				{#each sizeOptions as option (option.value)}
					<Select.Item value={option.value} label={option.label} title={option.title} />
				{/each}
			</Select.Group>
		</Select.Content>
	</Select.Root>

	<Select.Root type="single" value={filters.range} onValueChange={onRangeChange}>
		<Select.Trigger
			size="xs"
			variant="chip"
			data-active={filters.range !== 'all'}
			aria-label={t('app.images.filter_time')}
		>
			{ranges.find((range) => range.value === filters.range)?.label ?? t('app.images.range_all')}
		</Select.Trigger>
		<Select.Content>
			<Select.Group>
				{#each ranges as range (range.value)}
					<Select.Item value={range.value} label={range.label} />
				{/each}
			</Select.Group>
		</Select.Content>
	</Select.Root>

	{#if filters.range === 'custom'}
		<Field.Field orientation="horizontal" class="w-auto gap-1.5" data-invalid={rangeInvalid}>
			<Field.Label for={customFromId} class="text-dim text-xs font-medium">
				{t('app.images.range_from')}
			</Field.Label>
			<Input
				id={customFromId}
				type="date"
				variant="chip"
				class="w-fit"
				value={filters.from}
				aria-invalid={rangeInvalid}
				required
				onchange={(event) => setDay('from', event.currentTarget.value)}
			/>
		</Field.Field>
		<Field.Field orientation="horizontal" class="w-auto gap-1.5" data-invalid={rangeInvalid}>
			<Field.Label for={customThroughId} class="text-dim text-xs font-medium">
				{t('app.images.range_through')}
			</Field.Label>
			<Input
				id={customThroughId}
				type="date"
				variant="chip"
				class="w-fit"
				value={filters.through}
				aria-invalid={rangeInvalid}
				required
				onchange={(event) => setDay('through', event.currentTarget.value)}
			/>
		</Field.Field>
	{/if}

	{#if rangeInvalid}
		<Field.Error class="text-xs" role="alert">{t('app.images.range_reversed')}</Field.Error>
	{:else if rangeIncomplete}
		<Field.Error class="text-xs" role="status">{t('app.images.range_incomplete')}</Field.Error>
	{/if}

	{#if imageFiltersActive(filters)}
		<Button
			variant="ghost"
			size="icon-sm"
			class="text-muted-foreground hover:text-foreground size-[26px]"
			title={t('app.images.clear_filters')}
			aria-label={t('app.images.clear_filters')}
			onclick={() => {
				selectedModels = [];
				selectedSizes = [];
				onchange?.(clearImageFilters());
			}}
		>
			<XIcon class="size-3.5" />
		</Button>
	{/if}
</div>
