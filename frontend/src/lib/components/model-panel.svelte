<script lang="ts">
	import type { Component } from 'svelte';
	import type { IconProps } from '@lucide/svelte';
	import LassoIcon from '@lucide/svelte/icons/lasso';
	import LayersIcon from '@lucide/svelte/icons/layers';
	import Maximize2Icon from '@lucide/svelte/icons/maximize-2';
	import MessageSquareTextIcon from '@lucide/svelte/icons/message-square-text';
	import SearchIcon from '@lucide/svelte/icons/search';
	import SparklesIcon from '@lucide/svelte/icons/sparkles';
	import TypeIcon from '@lucide/svelte/icons/type';
	import ZapIcon from '@lucide/svelte/icons/zap';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { formatMs } from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';
	import { addModel, modelIsRemoved, removeModel, studio, type Model } from '$lib/studio.svelte';
	import { useStudioHeader } from '$lib/studio-chrome.svelte';
	import { offerUndo } from '$lib/studio-toasts';

	const CAPABILITIES = ['text_to_image', 'image_to_image', 'upscale', 'realtime'] as const;
	type Filter = 'all' | (typeof CAPABILITIES)[number];
	// One icon per capability; the edit and captioning values are not
	// advertised by any model yet but get their icon when one does.
	const CAPABILITY_ICONS: Record<string, Component<IconProps>> = {
		text_to_image: TypeIcon,
		image_to_image: LayersIcon,
		upscale: Maximize2Icon,
		realtime: ZapIcon,
		inpaint: LassoIcon,
		mask: LassoIcon,
		masks: LassoIcon,
		edit: LassoIcon,
		image_to_text: MessageSquareTextIcon,
		caption: MessageSquareTextIcon
	};

	let query = $state('');
	let filter = $state<Filter>('all');

	const allRemoved = $derived(
		studio.models.length > 0 && studio.models.every((model) => modelIsRemoved(model.id))
	);
	const shownInPickers = $derived(studio.models.filter((model) => !modelIsRemoved(model.id)));
	const fastestRealtime = $derived(
		studio.models
			.filter((model) => model.realtime_p95_ms != null)
			.sort((a, b) => (a.realtime_p95_ms ?? 0) - (b.realtime_p95_ms ?? 0))[0]
	);
	const fastestTextToImage = $derived(
		studio.models
			.filter(
				(model) =>
					model.capabilities.includes('text_to_image') && model.estimated_gpu_ms_default != null
			)
			.sort((a, b) => (a.estimated_gpu_ms_default ?? 0) - (b.estimated_gpu_ms_default ?? 0))[0]
	);
	function capabilityIcon(capability: string): Component<IconProps> {
		return CAPABILITY_ICONS[capability] ?? SparklesIcon;
	}

	const filters = $derived<{ value: Filter; label: string; count: number }[]>([
		{ value: 'all', label: t('app.models.filter_all'), count: studio.models.length },
		...CAPABILITIES.map((capability) => ({
			value: capability,
			label: capabilityLabel(capability),
			count: studio.models.filter((model) => model.capabilities.includes(capability)).length
		})).filter((option) => option.count > 0)
	]);
	const visibleModels = $derived(
		studio.models.filter((model) => {
			if (filter !== 'all' && !model.capabilities.includes(filter)) return false;
			const needle = query.trim().toLowerCase();
			return (
				needle === '' ||
				model.name.toLowerCase().includes(needle) ||
				model.id.toLowerCase().includes(needle)
			);
		})
	);

	function capabilityLabel(capability: string): string {
		const labels: Record<string, string> = {
			text_to_image: t('app.models.cap_text_to_image'),
			image_to_image: t('app.models.cap_image_to_image'),
			upscale: t('app.models.cap_upscale'),
			realtime: t('app.models.cap_realtime')
		};
		return labels[capability] ?? capability.replaceAll('_', ' ');
	}

	function factorEstimates(model: Model): [string, number][] {
		return Object.entries(model.estimated_gpu_ms_by_factor ?? {}).sort(
			([left], [right]) => Number(left) - Number(right)
		);
	}

	/** The cost a person would compare: a realtime frame's p95, an upscale's
	 * time per factor, or a job's default GPU time. */
	function gpuTime(model: Model): string {
		if (model.realtime_p95_ms != null) return `${Math.round(model.realtime_p95_ms)} ms p95`;
		const factors = factorEstimates(model);
		if (factors.length > 0)
			return factors.map(([factor, ms]) => `×${factor} ${formatMs(ms)}`).join(' · ');
		if (model.estimated_gpu_ms_default != null) return formatMs(model.estimated_gpu_ms_default);
		return t('app.models.not_measured');
	}

	/** Hiding is local and instant, so there is nothing to roll back; the
	 * toast offers the way back without hunting for the row again. */
	function hideModel(model: Model): void {
		removeModel(model.id);
		offerUndo(
			t('app.toast.model_hidden').replace('{name}', () => model.name),
			() => addModel(model.id)
		);
	}

	useStudioHeader(() => ({ actions: filterField }));
</script>

{#snippet filterField()}
	<label
		class="bg-field border-line-strong focus-within:ring-ring/60 hidden h-9 w-64 items-center gap-2 rounded-md border px-2.5 focus-within:ring-2 md:flex"
	>
		<SearchIcon class="text-dim size-3.5 shrink-0" aria-hidden="true" />
		<span class="sr-only">{t('app.models.filter')}</span>
		<input
			type="search"
			bind:value={query}
			placeholder={t('app.models.filter')}
			class="placeholder:text-dim min-w-0 flex-1 bg-transparent text-sm outline-none"
		/>
	</label>
{/snippet}

{#snippet modelName(model: Model)}
	<div class="flex min-w-0 items-center gap-2.5">
		<span
			class="bg-raised ring-line-strong grid size-7 shrink-0 place-items-center rounded-md text-xs font-semibold ring-1 ring-inset"
			aria-hidden="true">{model.name.trim().charAt(0).toUpperCase()}</span
		>
		<div class="min-w-0 leading-tight">
			<p class="truncate font-medium">{model.name}</p>
			<p class="text-dim truncate font-mono text-xs">{model.id}</p>
		</div>
	</div>
{/snippet}

{#snippet capabilityMarks(model: Model)}
	{#each model.capabilities as capability (capability)}
		{@const Icon = capabilityIcon(capability)}
		{@const label = capabilityLabel(capability)}
		<li>
			<!-- The filter pills above name every icon, so the icons
			     are not tab stops; the tooltip serves the pointer and
			     the hidden text names each one to assistive tech. -->
			<Tooltip.Root>
				<Tooltip.Trigger tabindex={-1}>
					{#snippet child({ props })}
						<span
							{...props}
							class={'inline-grid size-6 place-items-center rounded-md border ' +
								(capability === 'realtime'
									? 'border-status-running/35 text-status-running'
									: 'border-line-strong text-muted-foreground')}
						>
							<Icon class="size-3.5" aria-hidden="true" />
							<span class="sr-only">{label}</span>
						</span>
					{/snippet}
				</Tooltip.Trigger>
				<Tooltip.Content>{label}</Tooltip.Content>
			</Tooltip.Root>
		</li>
	{/each}
{/snippet}

{#snippet pickerSwitch(model: Model, removed: boolean)}
	<button
		type="button"
		role="switch"
		aria-checked={!removed}
		aria-label={`${t('app.models.show_in_pickers')}: ${model.name}`}
		class="group hit-slop relative flex shrink-0 items-center gap-2 rounded outline-none [--hit-slop-x:0px] focus-visible:ring-2 focus-visible:ring-ring"
		onclick={() => (removed ? addModel(model.id) : hideModel(model))}
	>
		<span
			class={'relative h-4 w-7 shrink-0 rounded-full transition-colors ' +
				(removed ? 'bg-line-strong' : 'bg-brand')}
		>
			<span
				class={'absolute top-0.5 size-3 rounded-full transition-[left] ' +
					(removed ? 'left-0.5 bg-[#9aa0ad]' : 'left-[14px] bg-white')}
			></span>
		</span>
		<span class={removed ? 'text-muted-foreground' : ''}>
			{removed ? t('app.models.hidden') : t('app.models.in_pickers')}
		</span>
	</button>
{/snippet}

<div class="no-scrollbar h-full overflow-y-auto">
	<div class="flex w-full flex-col gap-5 px-4 py-6 md:px-6 md:py-7">
		<div>
			<h1 class="text-2xl font-semibold tracking-[-0.02em]">{t('app.models.title')}</h1>
			<p class="text-muted-foreground mt-1 max-w-3xl text-sm leading-relaxed">
				{t('app.models.sub')}
			</p>
		</div>

		{#if allRemoved}
			<div class="border-border bg-raised rounded-xl border px-4 py-3 text-sm">
				<p class="font-medium">{t('app.models.all_removed_title')}</p>
				<p class="text-muted-foreground mt-1">{t('app.models.all_removed_sub')}</p>
			</div>
		{/if}

		{#if !studio.modelsLoaded}
			<div role="status" class="flex flex-col gap-4">
				<span class="sr-only">{t('app.models.loading')}</span>
				<div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-hidden="true">
					{#each [0, 1, 2] as index (index)}
						<Skeleton class="h-[86px] rounded-xl" />
					{/each}
				</div>
				<Skeleton class="h-8 w-72 max-w-full rounded-lg" aria-hidden="true" />
				<div class="bg-card border-border rounded-xl border" aria-hidden="true">
					<Skeleton class="m-3 h-3 w-40 rounded" />
					{#each [0, 1, 2, 3, 4] as index (index)}
						<div class="border-border flex h-11 items-center gap-2.5 border-t px-3">
							<Skeleton class="size-7 rounded-md" />
							<Skeleton class="h-3 w-40 rounded" />
							<span class="flex-1"></span>
							<Skeleton class="h-3 w-20 rounded" />
							<Skeleton class="h-4 w-7 rounded-full" />
						</div>
					{/each}
				</div>
			</div>
		{:else if studio.models.length === 0}
			<div class="bg-card border-border text-muted-foreground rounded-xl border p-6 text-sm">
				{t('app.models.empty')}
			</div>
		{:else}
			<div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
				<!-- On phones the count is one compact row; from md it is a KPI card. -->
				<div
					class="bg-card border-border flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-xl border px-4 py-2.5 md:block md:py-3.5"
				>
					<p class="text-muted-foreground text-xs">{t('app.models.registered')}</p>
					<p class="text-base leading-tight font-medium tabular-nums md:mt-1.5 md:text-[22px]">
						{studio.models.length}
					</p>
					<p class="text-dim text-xs md:mt-1">
						{t('app.models.shown_in_pickers').replace('{count}', String(shownInPickers.length))}
					</p>
				</div>
				{#if fastestRealtime?.realtime_p95_ms != null}
					<div class="bg-card border-border rounded-xl border px-4 py-3.5">
						<p class="text-muted-foreground text-xs">{t('app.models.fastest_realtime')}</p>
						<p class="mt-1.5 text-[22px] leading-tight font-medium tabular-nums">
							{Math.round(fastestRealtime.realtime_p95_ms)} ms
						</p>
						<p class="text-dim mt-1 truncate text-xs">{fastestRealtime.name} · p95</p>
					</div>
				{/if}
				{#if fastestTextToImage?.estimated_gpu_ms_default != null}
					<div class="bg-card border-border rounded-xl border px-4 py-3.5">
						<p class="text-muted-foreground text-xs">{t('app.models.fastest_text_to_image')}</p>
						<p class="mt-1.5 text-[22px] leading-tight font-medium tabular-nums">
							{formatMs(fastestTextToImage.estimated_gpu_ms_default)}
						</p>
						<p class="text-dim mt-1 truncate text-xs">
							{fastestTextToImage.name} · {t('app.models.gpu_estimate')}
						</p>
					</div>
				{/if}
			</div>

			<div
				class="border-line-strong flex w-fit max-w-full flex-wrap gap-1.5 md:bg-field md:gap-0.5 md:rounded-lg md:border md:p-0.5"
				role="group"
				aria-label={t('app.models.filter_capability')}
			>
				{#each filters as option (option.value)}
					<button
						type="button"
						class={'border-line-strong flex h-8 shrink-0 items-center gap-1 rounded-[6px] border px-2.5 text-xs font-medium whitespace-nowrap outline-none focus-visible:ring-2 focus-visible:ring-ring md:h-7 md:border-transparent ' +
							(filter === option.value
								? 'bg-hover text-foreground ring-line-strong ring-1 ring-inset'
								: 'text-muted-foreground hover:text-foreground')}
						aria-pressed={filter === option.value}
						onclick={() => (filter = option.value)}
					>
						{#if option.value !== 'all'}
							{@const Icon = capabilityIcon(option.value)}
							<Icon class="size-3.5 shrink-0" aria-hidden="true" />
						{/if}
						{option.label}
						<span class="tabular-nums">{option.count}</span>
					</button>
				{/each}
			</div>

			<div class="md:hidden">
				<label
					class="bg-field border-line-strong flex h-9 items-center gap-2 rounded-md border px-2.5"
				>
					<SearchIcon class="text-dim size-3.5 shrink-0" aria-hidden="true" />
					<span class="sr-only">{t('app.models.filter')}</span>
					<input
						type="search"
						bind:value={query}
						placeholder={t('app.models.filter')}
						class="placeholder:text-dim min-w-0 flex-1 bg-transparent text-sm outline-none"
					/>
				</label>
			</div>

			<ul class="flex flex-col gap-2 md:hidden">
				{#each visibleModels as model (model.id)}
					{@const removed = modelIsRemoved(model.id)}
					<li class="bg-card border-border rounded-xl border p-3">
						<div class="flex items-center justify-between gap-3">
							<div class="min-w-0 flex-1">
								{@render modelName(model)}
							</div>
							{@render pickerSwitch(model, removed)}
						</div>
						<dl class="mt-3 grid grid-cols-2 gap-x-4 gap-y-2.5 text-sm">
							<div class="col-span-2">
								<dt class="text-muted-foreground text-xs">{t('app.models.col_capabilities')}</dt>
								<dd class="mt-1">
									<ul class="flex flex-wrap gap-1">
										{@render capabilityMarks(model)}
									</ul>
								</dd>
							</div>
							<div>
								<dt class="text-muted-foreground text-xs">{t('app.models.vram')}</dt>
								<dd class="tabular-nums">{model.min_vram_gb} GB</dd>
							</div>
							<div>
								<dt class="text-muted-foreground text-xs">{t('app.models.col_gpu_time')}</dt>
								<dd
									class={'tabular-nums ' +
										(gpuTime(model) === t('app.models.not_measured')
											? 'text-muted-foreground'
											: '')}
								>
									{gpuTime(model)}
								</dd>
							</div>
						</dl>
					</li>
				{:else}
					<li
						class="bg-card border-border text-muted-foreground rounded-xl border px-3 py-6 text-center text-sm"
					>
						{t('app.models.no_match')}
					</li>
				{/each}
			</ul>

			<div
				class="bg-card border-border hidden overflow-x-auto rounded-xl border md:block"
				role="region"
				aria-label={t('app.models.title')}
			>
				<table class="w-full min-w-[760px] border-collapse text-sm">
					<thead>
						<tr class="text-muted-foreground border-border border-b text-start text-xs">
							<th class="h-10 px-3 text-start font-medium">{t('app.models.col_model')}</th>
							<th class="h-10 px-3 text-start font-medium">{t('app.models.col_capabilities')}</th>
							<th class="h-10 px-3 text-start font-medium">{t('app.models.vram')}</th>
							<th class="h-10 px-3 text-start font-medium">{t('app.models.col_gpu_time')}</th>
							<th class="h-10 px-3 text-start font-medium">{t('app.models.col_pickers')}</th>
						</tr>
					</thead>
					<tbody>
						{#each visibleModels as model (model.id)}
							{@const removed = modelIsRemoved(model.id)}
							<tr class="border-border border-b last:border-b-0">
								<td class="h-14 px-3">
									{@render modelName(model)}
								</td>
								<td class="px-3">
									<ul class="flex flex-wrap gap-1">
										{@render capabilityMarks(model)}
									</ul>
								</td>
								<td class="px-3 tabular-nums">{model.min_vram_gb} GB</td>
								<td
									class={'px-3 tabular-nums ' +
										(gpuTime(model) === t('app.models.not_measured')
											? 'text-muted-foreground'
											: '')}>{gpuTime(model)}</td
								>
								<td class="px-3">
									{@render pickerSwitch(model, removed)}
								</td>
							</tr>
						{:else}
							<tr>
								<td colspan="5" class="text-muted-foreground px-3 py-6 text-center">
									{t('app.models.no_match')}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</div>
</div>
