<script lang="ts">
	import ClipboardPasteIcon from '@lucide/svelte/icons/clipboard-paste';
	import PencilIcon from '@lucide/svelte/icons/pencil';
	import ScanLineIcon from '@lucide/svelte/icons/scan-line';
	import StarIcon from '@lucide/svelte/icons/star';
	import Trash2Icon from '@lucide/svelte/icons/trash-2';
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import * as ToggleGroup from '$lib/components/ui/toggle-group';
	import ParamSliderField from '$lib/components/param-slider-field.svelte';
	import {
		defaultSizeIndex,
		enumIndexToNorm,
		formatParamValue,
		guidanceSpec,
		normToEnumIndex,
		normToValue,
		sizeOptions as modelSizeOptions,
		stepsSpec,
		valueToNorm
	} from '$lib/model-params';
	import { formatMs } from '$lib/benchmark';
	import { estimateGpuMs } from '$lib/gpu-estimate';
	import {
		generationById,
		isStarred,
		loadHistory,
		pollWhileWorking,
		studio,
		toggleStarred
	} from '$lib/studio.svelte';
	import HistoryStrip from '$lib/components/history-strip.svelte';

	// Matches the Input component's field styling for the native controls it
	// does not vendor (select, textarea).
	const fieldClass =
		'dark:bg-input/30 border-input focus-visible:border-ring focus-visible:ring-ring/50 ' +
		'placeholder:text-muted-foreground w-full min-w-0 rounded-lg border bg-transparent ' +
		'px-2.5 py-1 text-base transition-colors outline-none focus-visible:ring-3 md:text-sm ' +
		'disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50';

	let stepsNorm = $state(0);
	let guidanceNorm = $state(0);
	let sizeIndex = $state(0);
	let sizeContext = $state({ modelId: '', optionCount: 0 });
	let count = $state('1');
	let normsReady = $state(false);
	let seed = $state('');
	let sourceAssetId = $state<string | null>(null);
	let errorText = $state('');

	// The viewer shows the clicked generation, or the newest finished one.
	const shown = $derived(
		(() => {
			if (studio.selectedId) {
				const selected = generationById(studio.selectedId);
				if (selected !== undefined && selected.assets.length > 0) return selected;
			}
			return (
				studio.history.find((g) => g.assets.length > 0) ??
				studio.starredExtras.find((g) => g.assets.length > 0) ??
				null
			);
		})()
	);
	// Jobs queue server side; submitting never blocks the form (docs/blueprint.md,
	// the generation request path returns a job id immediately).
	const working = $derived(
		studio.history.filter((g) => g.state === 'queued' || g.state === 'running').length
	);
	const runningProgress = $derived(
		studio.history.find((g) => g.state === 'running' && g.progress !== null)?.progress ?? null
	);
	const shownPrompt = $derived((shown?.params.prompt ?? '').trim());
	const shownStarred = $derived(shown !== null && isStarred(shown.id));

	// The manifest schema decides which resolutions a model supports
	// (docs/architecture.md, model manifests); no enum means unconstrained.
	const selectedModel = $derived(studio.models.find((m) => m.id === studio.modelId));
	const canEdit = $derived(
		shown !== null &&
			shown.assets.length > 0 &&
			(selectedModel?.capabilities.includes('image_to_image') ?? false)
	);
	const stepsRange = $derived(stepsSpec(selectedModel));
	const guidanceRange = $derived(guidanceSpec(selectedModel));
	const sizeOptions = $derived(modelSizeOptions(selectedModel));
	const stepsValue = $derived(normToValue(stepsNorm, stepsRange));
	const guidanceValue = $derived(normToValue(guidanceNorm, guidanceRange));
	const sizeValue = $derived(sizeOptions[sizeIndex] ?? sizeOptions[0]);
	const sizeKey = $derived(String(sizeValue));
	const gpuEstimateMs = $derived(
		estimateGpuMs(selectedModel, {
			steps: stepsValue,
			width: sizeValue,
			height: sizeValue
		})
	);
	const gpuEstimateLabel = $derived(gpuEstimateMs != null ? `~${formatMs(gpuEstimateMs)}` : null);

	$effect(() => {
		if (!selectedModel?.capabilities.includes('image_to_image')) {
			sourceAssetId = null;
		}
	});

	$effect(() => {
		if (!selectedModel) return;
		const count = sizeOptions.length;
		if (count === 0) return;

		if (sizeContext.modelId === '') {
			sizeIndex = defaultSizeIndex(selectedModel, sizeOptions);
			sizeContext = { modelId: selectedModel.id, optionCount: count };
			return;
		}

		if (sizeContext.modelId !== selectedModel.id) {
			if (sizeContext.optionCount > 1 && count > 1) {
				sizeIndex = normToEnumIndex(enumIndexToNorm(sizeIndex, sizeContext.optionCount), count);
			} else {
				sizeIndex = defaultSizeIndex(selectedModel, sizeOptions);
			}
			sizeContext = { modelId: selectedModel.id, optionCount: count };
			return;
		}

		if (sizeIndex >= count) sizeIndex = count - 1;
	});

	$effect(() => {
		if (!selectedModel || normsReady) return;
		stepsNorm = valueToNorm(stepsRange.default, stepsRange);
		guidanceNorm = valueToNorm(guidanceRange.default, guidanceRange);
		normsReady = true;
	});

	async function generate(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		errorText = '';
		studio.selectedId = null; // let fresh results take the viewer back
		const jobs = Math.min(Math.max(Number(count) || 1, 1), 8);
		for (let index = 0; index < jobs; index += 1) {
			const params: Record<string, unknown> = {
				prompt: studio.prompt,
				steps: stepsValue,
				guidance: guidanceValue,
				width: sizeValue,
				height: sizeValue
			};
			if (seed.trim() !== '') params.seed = Number(seed) + index;
			const response = await fetch('/api/v1/generations', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({
					model_id: studio.modelId,
					params,
					...(sourceAssetId && selectedModel?.capabilities.includes('image_to_image')
						? { source_asset_id: sourceAssetId }
						: {})
				})
			});
			if (!response.ok) {
				const body = (await response.json().catch(() => null)) as { detail?: string } | null;
				errorText = body?.detail ?? response.statusText;
				break;
			}
		}
		await loadHistory();
		void pollWhileWorking();
	}

	function insertPrompt(): void {
		if (shownPrompt !== '') studio.prompt = shownPrompt;
	}

	function starShown(): void {
		if (shown !== null) toggleStarred(shown.id);
	}

	function editShown(): void {
		if (shown === null || shown.assets.length === 0) return;
		const assetId = shown.assets[0].id;
		if (sourceAssetId === assetId) {
			sourceAssetId = null;
			return;
		}
		sourceAssetId = assetId;
		if (shownPrompt !== '') studio.prompt = shownPrompt;
	}

	function onSizeChange(value: string): void {
		const index = sizeOptions.indexOf(Number(value));
		if (index >= 0) sizeIndex = index;
	}
</script>

<div class="grid h-full min-h-0 gap-4 lg:grid-cols-[minmax(300px,380px)_1fr]">
	<Card.Root class="no-scrollbar flex min-h-0 flex-col overflow-y-auto">
		<Card.Header>
			<Card.Title>{t('app.gen.title')}</Card.Title>
			<Card.Description>{t('app.gen.sub')}</Card.Description>
		</Card.Header>
		<Card.Content class="flex min-h-0 flex-1 flex-col">
			{#if studio.models.length === 0}
				<p class="text-muted-foreground text-sm leading-relaxed">{t('app.gen.no_models')}</p>
			{:else}
				<form class="flex min-h-0 flex-1 flex-col gap-4" onsubmit={generate}>
					<div class="flex flex-col gap-2">
						<Label for="gen-model">{t('app.gen.model')}</Label>
						<select id="gen-model" class={fieldClass + ' h-8'} bind:value={studio.modelId}>
							{#each studio.models as model (model.id)}
								<option value={model.id}>
									{model.name}{model.estimated_gpu_ms_default != null
										? ` (~${formatMs(model.estimated_gpu_ms_default)})`
										: ''}
								</option>
							{/each}
						</select>
					</div>
					<div class="flex flex-col gap-2">
						<Label for="gen-prompt">{t('app.gen.prompt')}</Label>
						<textarea
							id="gen-prompt"
							class={fieldClass + ' no-scrollbar h-44 resize-none overflow-y-auto py-2'}
							placeholder={t('app.gen.prompt_placeholder')}
							bind:value={studio.prompt}></textarea>
					</div>
					<div class="grid grid-cols-2 gap-3">
						<div class="flex flex-col gap-2">
							<Label for="gen-count">{t('app.gen.count')}</Label>
							<Input id="gen-count" type="number" min="1" max="8" bind:value={count} />
						</div>
						<div class="flex flex-col gap-2">
							<Label for="gen-seed">{t('app.gen.seed')}</Label>
							<Input
								id="gen-seed"
								type="number"
								placeholder={t('app.gen.seed_placeholder')}
								bind:value={seed}
							/>
						</div>
					</div>
					<div class="flex flex-col gap-2">
						<Label>{t('app.gen.size')}</Label>
						<ToggleGroup.Root
							type="single"
							variant="outline"
							spacing={0}
							class="flex w-full"
							value={sizeKey}
							onValueChange={(value) => value && onSizeChange(value)}
						>
							{#each sizeOptions as option (option)}
								<ToggleGroup.Item value={String(option)} class="min-w-0 flex-1 text-xs">
									{option} x {option}
								</ToggleGroup.Item>
							{/each}
						</ToggleGroup.Root>
					</div>
					{#if normsReady}
						<div class="flex flex-col gap-4">
							<ParamSliderField
								id="gen-steps"
								label={t('app.gen.steps')}
								bind:norm={stepsNorm}
								minLabel={formatParamValue(stepsRange.min, stepsRange)}
								maxLabel={formatParamValue(stepsRange.max, stepsRange)}
								valueLabel={formatParamValue(stepsValue, stepsRange)}
							/>
							<ParamSliderField
								id="gen-guidance"
								label={t('app.gen.guidance')}
								bind:norm={guidanceNorm}
								minLabel={formatParamValue(guidanceRange.min, guidanceRange)}
								maxLabel={formatParamValue(guidanceRange.max, guidanceRange)}
								valueLabel={formatParamValue(guidanceValue, guidanceRange)}
							/>
						</div>
					{/if}
					<Button type="submit" disabled={studio.prompt.trim() === ''}>
						{t('app.gen.generate')}{gpuEstimateLabel != null ? ` ${gpuEstimateLabel}` : ''}
					</Button>
					<div class="border-border flex flex-col gap-2 border-t pt-4">
						<div class="grid grid-cols-2 gap-2">
							<Button
								type="button"
								variant="outline"
								size="sm"
								class="justify-start"
								disabled={shownPrompt === ''}
								onclick={insertPrompt}
							>
								<ClipboardPasteIcon />
								{t('app.gen.insert_prompt')}
							</Button>
							<Button
								type="button"
								variant={shownStarred ? 'secondary' : 'outline'}
								size="sm"
								class="justify-start"
								disabled={shown === null}
								onclick={starShown}
							>
								<StarIcon class={shownStarred ? 'fill-current' : ''} />
								{shownStarred ? t('app.gen.unstar') : t('app.gen.star')}
							</Button>
							<Button
								type="button"
								variant={sourceAssetId !== null ? 'secondary' : 'outline'}
								size="sm"
								class="justify-start"
								disabled={!canEdit}
								onclick={editShown}
							>
								<PencilIcon />
								{t('app.gen.edit')}
							</Button>
							<Button
								type="button"
								variant="outline"
								size="sm"
								class="justify-start"
								disabled
								title={t('app.gen.coming_soon')}
							>
								<ScanLineIcon />
								{t('app.gen.upscale')}
							</Button>
							<Button
								type="button"
								variant="outline"
								size="sm"
								class="col-span-2 justify-start"
								disabled
								title={t('app.gen.coming_soon')}
							>
								<Trash2Icon />
								{t('app.gen.delete')}
							</Button>
						</div>
					</div>
					{#if working > 0}
						<p class="text-muted-foreground text-sm">
							{working}
							{t('app.gen.working_suffix')}{runningProgress !== null
								? ` (${Math.round(runningProgress * 100)}%)`
								: ''}
						</p>
					{/if}
					{#if errorText !== ''}
						<p class="text-destructive text-sm leading-relaxed">{errorText}</p>
					{/if}
				</form>
			{/if}
		</Card.Content>
	</Card.Root>

	<!-- min-w-0: the thumbnail strip's intrinsic width must not widen the grid track -->
	<div class="flex min-h-0 min-w-0 flex-col gap-3">
		<Card.Root class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
			<Card.Content class="flex min-h-0 flex-1 flex-col gap-2 p-4">
				{#if shown !== null}
					<a
						href={shown.assets[0].url}
						target="_blank"
						rel="noopener"
						class="block min-h-0 flex-1"
						title={t('app.gen.open_full')}
					>
						<img
							src={shown.assets[0].url}
							alt={shown.params.prompt ?? t('app.gen.result')}
							class="h-full w-full rounded-lg object-contain"
						/>
					</a>
					<p class="text-muted-foreground min-w-0 truncate text-center text-xs">
						{shown.params.prompt}
					</p>
				{:else}
					<div
						class="text-foreground/55 grid h-full place-items-center px-6 text-center text-xs tracking-[0.14em] uppercase"
					>
						{t('app.gen.result_hint')}
					</div>
				{/if}
			</Card.Content>
		</Card.Root>
		<div class="shrink-0">
			<HistoryStrip />
		</div>
	</div>
</div>
