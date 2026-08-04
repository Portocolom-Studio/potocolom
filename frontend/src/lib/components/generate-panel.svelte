<script lang="ts">
	import ClipboardPasteIcon from '@lucide/svelte/icons/clipboard-paste';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import DownloadIcon from '@lucide/svelte/icons/download';
	import PencilIcon from '@lucide/svelte/icons/pencil';
	import ScanLineIcon from '@lucide/svelte/icons/scan-line';
	import StarIcon from '@lucide/svelte/icons/star';
	import Trash2Icon from '@lucide/svelte/icons/trash-2';
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import * as Select from '$lib/components/ui/select';
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
		strengthSpec,
		valueToNorm
	} from '$lib/model-params';
	import { formatMs } from '$lib/benchmark';
	import { estimateGpuMs, estimateUpscaleGpuMs } from '$lib/gpu-estimate';
	import { estimatePromptTokens, exceedsWindow } from '$lib/prompt-tokens';
	import {
		defaultUpscaleModelId,
		filterImageToImageModels,
		filterTextToImageModels,
		filterUpscaleModels,
		generationById,
		isStarred,
		loadHistory,
		openService,
		pollWhileWorking,
		selectGeneration,
		studio,
		toggleStarred,
		type GenerationLineage,
		type LineageEntry,
		type Model
	} from '$lib/studio.svelte';
	import HistoryStrip from '$lib/components/history-strip.svelte';

	let { mode }: { mode: 'generate' | 'image_to_image' | 'upscale' } = $props();

	// Matches the Input component's field styling for the native controls it
	// does not vendor (select, textarea).
	const fieldClass =
		'dark:bg-input/30 border-input focus-visible:border-ring focus-visible:ring-ring/50 ' +
		'placeholder:text-muted-foreground w-full min-w-0 rounded-lg border bg-transparent ' +
		'px-2.5 py-1 text-base transition-colors outline-none focus-visible:ring-3 md:text-sm ' +
		'disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50';
	// Default toggle `data-[state=on]:bg-muted` is nearly invisible on the card;
	// match LanguageToggle so Fast/Quality/factor picks read clearly.
	const toggleOnClass =
		'data-[state=on]:bg-primary data-[state=on]:text-primary-foreground data-[state=on]:hover:bg-primary/90';

	let stepsNorm = $state(0);
	let guidanceNorm = $state(0);
	let strengthNorm = $state(0);
	let sizeIndex = $state(0);
	let sizeContext = $state({ modelId: '', optionCount: 0 });
	let count = $state('1');
	let normsReady = $state(false);
	let seed = $state('');
	let sourceAssetId = $state<string | null>(null);
	let branchParams = $state<Record<string, unknown>>({});
	let upscaleFactor = $state(2);
	let errorText = $state('');
	let lineage = $state<GenerationLineage | null>(null);

	// The viewer shows the clicked generation, or the newest finished one.
	const shown = $derived(
		(() => {
			if (studio.selectedId) {
				const selected = generationById(studio.selectedId);
				if (selected !== undefined) return selected;
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
	// The lineage fetch keys on this, not on shown: a history poll rebuilds every
	// generation object, so tracking shown itself would refetch and blank the
	// breadcrumb every 1.5 seconds while a job is running.
	const shownId = $derived(shown?.id ?? null);
	const shownThumbnail = $derived(shown?.assets[0]?.thumbnail_url ?? null);
	const shownAction = $derived(
		shown === null
			? null
			: shown.source_asset_id === null
				? 'generate'
				: studio.models
							.find((model) => model.id === shown.model_id)
							?.capabilities.includes('upscale')
					? 'upscale'
					: 'image_to_image'
	);

	const textToImageModels = $derived(filterTextToImageModels(studio.models));
	const imageToImageModels = $derived(filterImageToImageModels(studio.models));
	const diffusionModels = $derived(
		mode === 'image_to_image' ? imageToImageModels : textToImageModels
	);
	const upscaleModels = $derived(filterUpscaleModels(studio.models));
	const compatibleModelsRegistered = $derived(
		studio.models.some((model) =>
			model.capabilities.includes(
				mode === 'image_to_image'
					? 'image_to_image'
					: mode === 'upscale'
						? 'upscale'
						: 'text_to_image'
			)
		)
	);
	const activeModelId = $derived(
		mode === 'image_to_image' ? studio.imageToImageModelId : studio.modelId
	);
	const upscaleModelId = $derived(
		upscaleModels.some((model) => model.id === studio.upscaleModelId)
			? studio.upscaleModelId
			: defaultUpscaleModelId(studio.models)
	);
	const upscaleModel = $derived(upscaleModels.find((model) => model.id === upscaleModelId));
	const selectedModel = $derived(diffusionModels.find((model) => model.id === activeModelId));
	const sourceAsset = $derived(
		sourceAssetId === null
			? null
			: ([
					...studio.history,
					...studio.starredExtras,
					...(studio.selectedExtra === null ? [] : [studio.selectedExtra])
				]
					.flatMap((generation) => generation.assets)
					.find((asset) => asset.id === sourceAssetId) ?? null)
	);
	const canEdit = $derived(
		shown !== null && shown.assets.length > 0 && imageToImageModels.length > 0
	);
	const canUpscale = $derived(
		upscaleModel != null &&
			(sourceAsset !== null ||
				(shown !== null && shown.assets.length > 0 && shown.state === 'succeeded'))
	);
	const stepsRange = $derived(stepsSpec(selectedModel));
	const guidanceRange = $derived(guidanceSpec(selectedModel));
	const strengthRange = $derived(strengthSpec(selectedModel));
	const sizeOptions = $derived(modelSizeOptions(selectedModel));
	const stepsValue = $derived(normToValue(stepsNorm, stepsRange));
	const guidanceValue = $derived(normToValue(guidanceNorm, guidanceRange));
	const strengthValue = $derived(normToValue(strengthNorm, strengthRange));
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
	// Diffusers truncates at the text encoder window and only logs it worker
	// side, so the prompt tail silently stops affecting the image (issue #148).
	const promptWindow = $derived(selectedModel?.prompt_token_limit ?? 0);
	const promptTokens = $derived(estimatePromptTokens(studio.prompt));
	const promptTokenNotice = $derived(
		exceedsWindow(promptTokens, promptWindow)
			? t('app.gen.prompt_too_long')
					.replace('{tokens}', String(promptTokens))
					.replace('{limit}', String(promptWindow))
			: null
	);
	const upscaleSourceAsset = $derived(sourceAsset ?? shown?.assets[0] ?? null);
	const upscaleSource = $derived(
		upscaleSourceAsset !== null
			? { width: upscaleSourceAsset.width, height: upscaleSourceAsset.height }
			: undefined
	);
	const upscaleEstimateMs = $derived(
		estimateUpscaleGpuMs(upscaleModel, upscaleFactor, upscaleSource)
	);
	const upscaleEstimateLabel = $derived(
		upscaleEstimateMs != null ? `~${formatMs(upscaleEstimateMs)}` : null
	);
	const upscaleOutLabel = $derived(
		upscaleSource != null
			? `${upscaleSource.width * upscaleFactor}x${upscaleSource.height * upscaleFactor}`
			: null
	);
	const upscaleChoiceLabel = $derived(
		upscaleModel != null
			? `${upscaleModel.name} - x${upscaleFactor}` +
					(upscaleSource != null
						? ` - ${upscaleSource.width}x${upscaleSource.height}` +
							(upscaleOutLabel != null ? ` -> ${upscaleOutLabel}` : '')
						: '') +
					(upscaleEstimateLabel != null ? ` - ${upscaleEstimateLabel}` : '')
			: null
	);
	const shownFactor = $derived(
		typeof shown?.params.factor === 'number' ? shown.params.factor : null
	);
	const panelTitle = $derived(
		mode === 'generate'
			? t('app.gen.title')
			: mode === 'image_to_image'
				? t('app.image_to_image.title')
				: t('app.upscale.title')
	);
	const panelSub = $derived(
		mode === 'generate'
			? t('app.gen.sub')
			: mode === 'image_to_image'
				? t('app.image_to_image.sub')
				: t('app.upscale.sub')
	);
	$effect(() => {
		const prefill = studio.generationPrefill;
		if (prefill === null || prefill.mode !== mode) return;
		studio.generationPrefill = null;
		studio.prompt = prefill.prompt;
		sourceAssetId = prefill.sourceAssetId;
		seed = '';
		branchParams = { ...prefill.params };
		delete branchParams.seed;
		if (mode === 'upscale') {
			studio.upscaleModelId = prefill.modelId;
			const factor = Number(prefill.params.factor);
			if (factor === 2 || factor === 4) upscaleFactor = factor;
			return;
		}

		if (mode === 'image_to_image') {
			studio.imageToImageModelId = prefill.modelId;
		} else {
			studio.modelId = prefill.modelId;
		}
		const model = studio.models.find((item) => item.id === prefill.modelId);
		if (!model) return;
		const steps = stepsSpec(model);
		const guidance = guidanceSpec(model);
		const strength = strengthSpec(model);
		stepsNorm = valueToNorm(
			typeof prefill.params.steps === 'number' ? prefill.params.steps : steps.default,
			steps
		);
		guidanceNorm = valueToNorm(
			typeof prefill.params.guidance === 'number' ? prefill.params.guidance : guidance.default,
			guidance
		);
		strengthNorm = valueToNorm(
			typeof prefill.params.strength === 'number' ? prefill.params.strength : strength.default,
			strength
		);
		const options = modelSizeOptions(model);
		const requestedSize =
			prefill.params.width === prefill.params.height ? Number(prefill.params.width) : NaN;
		const requestedIndex = options.indexOf(requestedSize);
		sizeIndex = requestedIndex >= 0 ? requestedIndex : defaultSizeIndex(model, options);
		sizeContext = { modelId: model.id, optionCount: options.length };
		normsReady = true;
	});

	$effect(() => {
		const jobId = shownId;
		lineage = null;
		if (!jobId) return;
		let cancelled = false;
		void fetch(`/api/v1/generations/${jobId}/lineage`)
			.then(async (response) => {
				if (!response.ok) return;
				const loaded = (await response.json()) as GenerationLineage;
				if (!cancelled) lineage = loaded;
			})
			.catch(() => {
				// The detail remains usable when lineage cannot load.
			});
		return () => {
			cancelled = true;
		};
	});

	$effect(() => {
		if (mode === 'image_to_image' && !selectedModel?.capabilities.includes('image_to_image')) {
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
		strengthNorm = valueToNorm(strengthRange.default, strengthRange);
		normsReady = true;
	});

	async function generate(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		errorText = '';
		studio.selectedId = null; // let fresh results take the viewer back
		const jobs = Math.min(Math.max(Number(count) || 1, 1), 8);
		for (let index = 0; index < jobs; index += 1) {
			const params: Record<string, unknown> = {
				...branchParams,
				prompt: studio.prompt,
				steps: stepsValue,
				guidance: guidanceValue,
				width: sizeValue,
				height: sizeValue
			};
			if (mode === 'image_to_image') params.strength = strengthValue;
			if (seed.trim() !== '') params.seed = Number(seed) + index;
			const response = await fetch('/api/v1/generations', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({
					model_id: activeModelId,
					params,
					...(mode === 'image_to_image' && sourceAssetId ? { source_asset_id: sourceAssetId } : {})
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

	async function upscaleShown(): Promise<void> {
		if (!canUpscale || upscaleSourceAsset === null || upscaleModel == null) return;
		// Capture before clearing the selection: `shown` is derived from
		// selectedId, so reading it afterwards would target the newest
		// generation instead of the one on screen.
		const sourceId = upscaleSourceAsset.id;
		const modelId = upscaleModel.id;
		errorText = '';
		studio.selectedId = null;
		const response = await fetch('/api/v1/generations', {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({
				model_id: modelId,
				params: { ...branchParams, factor: upscaleFactor },
				source_asset_id: sourceId
			})
		});
		if (!response.ok) {
			const body = (await response.json().catch(() => null)) as { detail?: string } | null;
			errorText = body?.detail ?? response.statusText;
			return;
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
		openService('image_to_image');
	}

	function onSizeChange(value: string): void {
		const index = sizeOptions.indexOf(Number(value));
		if (index >= 0) sizeIndex = index;
	}

	function onModelChange(value: string): void {
		if (mode === 'image_to_image') {
			studio.imageToImageModelId = value;
		} else {
			studio.modelId = value;
		}
	}

	function onUpscaleFactorChange(value: string): void {
		const factor = Number(value);
		if (factor === 2 || factor === 4) upscaleFactor = factor;
	}

	function onUpscaleModelChange(value: string): void {
		if (upscaleModels.some((model) => model.id === value)) studio.upscaleModelId = value;
	}

	function modelOptionLabel(model: Model | undefined): string {
		if (!model) return t('app.gen.model');
		return model.estimated_gpu_ms_default != null
			? `${model.name} (~${formatMs(model.estimated_gpu_ms_default)})`
			: model.name;
	}

	function lineageActionLabel(action: LineageEntry['action']): string {
		return t(`app.lineage.${action}`);
	}

	function selectLineage(entry: LineageEntry): void {
		if (entry.job_id !== null) void selectGeneration(entry.job_id);
	}
</script>

<div class="grid h-full min-h-0 gap-4 lg:grid-cols-[minmax(300px,380px)_1fr]">
	<Card.Root class="no-scrollbar flex min-h-0 flex-col overflow-y-auto">
		<Card.Header class="gap-3">
			<div class="flex flex-col gap-1.5">
				<Card.Title>{panelTitle}</Card.Title>
				<Card.Description>{panelSub}</Card.Description>
			</div>
		</Card.Header>
		<Card.Content class="flex min-h-0 flex-1 flex-col">
			{#if mode !== 'upscale'}
				{#if diffusionModels.length === 0}
					<p class="text-muted-foreground text-sm leading-relaxed">
						{compatibleModelsRegistered ? t('app.models.none_available') : t('app.gen.no_models')}
					</p>
				{:else}
					<form class="flex min-h-0 flex-1 flex-col gap-4" onsubmit={generate}>
						<div class="flex flex-col gap-2">
							<Label for="gen-model">{t('app.gen.model')}</Label>
							<Select.Root
								type="single"
								value={activeModelId}
								onValueChange={(value) => value && onModelChange(value)}
							>
								<Select.Trigger id="gen-model" class="w-full" size="sm">
									{modelOptionLabel(
										diffusionModels.find((model) => model.id === activeModelId) ??
											diffusionModels[0]
									)}
								</Select.Trigger>
								<Select.Content>
									<Select.Group>
										{#each diffusionModels as model (model.id)}
											<Select.Item value={model.id} label={modelOptionLabel(model)} />
										{/each}
									</Select.Group>
								</Select.Content>
							</Select.Root>
							{#if selectedModel?.requires_attribution}
								<p class="text-muted-foreground text-xs">{selectedModel.requires_attribution}</p>
							{/if}
						</div>
						{#if mode === 'image_to_image'}
							<div class="flex flex-col gap-2">
								<Label>{t('app.image_to_image.source')}</Label>
								<div
									class="border-border bg-muted/20 flex min-h-20 items-center gap-3 rounded-lg border border-dashed p-3"
								>
									{#if sourceAsset !== null}
										<img
											src={sourceAsset.thumbnail_url ?? sourceAsset.url}
											alt={t('app.image_to_image.source')}
											class="size-14 rounded-md object-cover"
										/>
										<p class="min-w-0 flex-1 truncate text-sm">{t('app.image_to_image.ready')}</p>
									{:else}
										<p class="text-muted-foreground flex-1 text-sm">
											{t('app.image_to_image.source_empty')}
										</p>
									{/if}
								</div>
								<div class="flex flex-col gap-2">
									<Button
										type="button"
										variant="outline"
										size="sm"
										disabled={shown === null || shown.assets.length === 0}
										onclick={() => {
											if (shown !== null && shown.assets.length > 0) {
												sourceAssetId = shown.assets[0].id;
											}
										}}
									>
										{t('app.image_to_image.use_selected')}
									</Button>
									<Button
										type="button"
										variant="ghost"
										size="sm"
										disabled={sourceAssetId === null}
										onclick={() => (sourceAssetId = null)}
									>
										{t('app.image_to_image.clear')}
									</Button>
								</div>
							</div>
						{/if}
						<div class="flex flex-col gap-2">
							<Label for="gen-prompt">{t('app.gen.prompt')}</Label>
							<textarea
								id="gen-prompt"
								class={fieldClass + ' no-scrollbar h-44 resize-none overflow-y-auto py-2'}
								placeholder={t('app.gen.prompt_placeholder')}
								aria-describedby={promptTokenNotice ? 'gen-prompt-window' : undefined}
								bind:value={studio.prompt}></textarea>
							{#if promptTokenNotice}
								<!-- Described by the field rather than announced from a live region:
								the count changes on every keystroke, which a polite region would
								read out again and again while the user is still typing. -->
								<p id="gen-prompt-window" class="text-muted-foreground text-sm leading-relaxed">
									{promptTokenNotice}
								</p>
							{/if}
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
									<ToggleGroup.Item
										value={String(option)}
										class={`min-w-0 flex-1 text-xs ${toggleOnClass}`}
									>
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
								{#if mode === 'image_to_image'}
									<ParamSliderField
										id="gen-strength"
										label={t('app.image_to_image.strength')}
										bind:norm={strengthNorm}
										minLabel={formatParamValue(strengthRange.min, strengthRange)}
										maxLabel={formatParamValue(strengthRange.max, strengthRange)}
										valueLabel={formatParamValue(strengthValue, strengthRange)}
									/>
								{/if}
							</div>
						{/if}
						<Button
							type="submit"
							disabled={studio.prompt.trim() === '' ||
								(mode === 'image_to_image' && sourceAssetId === null)}
						>
							{mode === 'image_to_image'
								? t('app.image_to_image.generate')
								: t('app.gen.generate')}{gpuEstimateLabel != null ? ` ${gpuEstimateLabel}` : ''}
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
									{t('app.image_to_image.use_action')}
								</Button>
								<Button
									type="button"
									variant="outline"
									size="sm"
									class="justify-start"
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
			{:else if upscaleModels.length === 0}
				<p class="text-muted-foreground text-sm leading-relaxed">
					{compatibleModelsRegistered ? t('app.models.none_available') : t('app.upscale.no_models')}
				</p>
			{:else if !canUpscale}
				<div class="flex min-h-0 flex-1 flex-col gap-4">
					<p class="text-muted-foreground text-sm leading-relaxed">{t('app.upscale.need_image')}</p>
					{#if shown !== null}
						<Button
							type="button"
							variant={shownStarred ? 'secondary' : 'outline'}
							size="sm"
							class="w-fit justify-start"
							onclick={starShown}
						>
							<StarIcon class={shownStarred ? 'fill-current' : ''} />
							{shownStarred ? t('app.gen.unstar') : t('app.gen.star')}
						</Button>
					{/if}
				</div>
			{:else}
				<div class="flex min-h-0 flex-1 flex-col gap-4">
					<div class="flex flex-col gap-2">
						<Label>{t('app.upscale.source')}</Label>
						<p class="text-muted-foreground truncate text-sm">
							{shownPrompt !== '' ? shownPrompt : t('app.gen.result')}
						</p>
					</div>
					<div class="flex flex-col gap-2">
						<Label for="upscale-model">{t('app.gen.upscaler')}</Label>
						<Select.Root
							type="single"
							value={upscaleModelId}
							onValueChange={(value) => value && onUpscaleModelChange(value)}
						>
							<Select.Trigger id="upscale-model" class="w-full" size="sm">
								{modelOptionLabel(upscaleModel)}
							</Select.Trigger>
							<Select.Content>
								<Select.Group>
									{#each upscaleModels as model (model.id)}
										<Select.Item value={model.id} label={modelOptionLabel(model)} />
									{/each}
								</Select.Group>
							</Select.Content>
						</Select.Root>
						{#if upscaleChoiceLabel != null}
							<p class="text-muted-foreground text-xs leading-relaxed">{upscaleChoiceLabel}</p>
						{/if}
					</div>
					<div class="flex flex-col gap-2">
						<Label>{t('app.upscale.factor')}</Label>
						<ToggleGroup.Root
							type="single"
							variant="outline"
							spacing={0}
							class="flex w-full"
							value={String(upscaleFactor)}
							onValueChange={(value) => value && onUpscaleFactorChange(value)}
						>
							<ToggleGroup.Item value="2" class={`min-w-0 flex-1 text-xs ${toggleOnClass}`}>
								{t('app.gen.upscale_x2')}
							</ToggleGroup.Item>
							<ToggleGroup.Item value="4" class={`min-w-0 flex-1 text-xs ${toggleOnClass}`}>
								{t('app.gen.upscale_x4')}
							</ToggleGroup.Item>
						</ToggleGroup.Root>
					</div>
					<Button type="button" disabled={!canUpscale} onclick={upscaleShown}>
						<ScanLineIcon />
						{t('app.gen.upscale')}{upscaleEstimateLabel != null ? ` ${upscaleEstimateLabel}` : ''}
					</Button>
					<div class="border-border flex flex-col gap-2 border-t pt-4">
						<Button
							type="button"
							variant={shownStarred ? 'secondary' : 'outline'}
							size="sm"
							class="w-fit justify-start"
							disabled={shown === null}
							onclick={starShown}
						>
							<StarIcon class={shownStarred ? 'fill-current' : ''} />
							{shownStarred ? t('app.gen.unstar') : t('app.gen.star')}
						</Button>
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
				</div>
			{/if}
		</Card.Content>
	</Card.Root>

	<!-- min-w-0: the thumbnail strip's intrinsic width must not widen the grid track -->
	<div class="flex min-h-0 min-w-0 flex-col gap-3">
		<Card.Root class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
			<Card.Content class="flex min-h-0 flex-1 flex-col gap-2 p-4">
				{#if shown !== null}
					{#if lineage !== null}
						<div class="shrink-0">
							<p class="text-muted-foreground mb-1 text-[0.65rem] font-medium uppercase">
								{t('app.lineage.ancestors')}
							</p>
							<div
								class="no-scrollbar flex min-w-0 items-center gap-1 overflow-x-auto"
								aria-label={t('app.lineage.ancestors')}
							>
								{#each lineage.ancestors as entry (entry.asset_id)}
									<button
										type="button"
										class="border-border bg-muted/30 hover:bg-muted flex w-20 shrink-0 flex-col items-center gap-1 rounded-md border p-1 text-[0.6rem] disabled:cursor-default"
										disabled={entry.job_id === null}
										title={lineageActionLabel(entry.action)}
										onclick={() => selectLineage(entry)}
									>
										{#if entry.thumbnail_url !== null}
											<img
												src={entry.thumbnail_url}
												alt={lineageActionLabel(entry.action)}
												class="size-12 rounded object-cover"
											/>
										{:else}
											<span
												class="border-border text-muted-foreground grid size-12 place-items-center rounded border border-dashed px-1 text-center leading-tight"
											>
												{t('app.lineage.missing')}
											</span>
										{/if}
										<span class="w-full truncate">{lineageActionLabel(entry.action)}</span>
									</button>
									<ChevronRightIcon class="text-muted-foreground size-3 shrink-0" />
								{/each}
								<div
									class="border-primary bg-primary/10 flex w-20 shrink-0 flex-col items-center gap-1 rounded-md border p-1 text-[0.6rem]"
									aria-current="page"
								>
									{#if shownThumbnail !== null}
										<img
											src={shownThumbnail}
											alt={shown.params.prompt ?? t('app.gen.result')}
											class="size-12 rounded object-cover"
										/>
									{:else}
										<span
											class="border-border text-muted-foreground grid size-12 place-items-center rounded border border-dashed px-1 text-center leading-tight"
										>
											{t('app.lineage.missing')}
										</span>
									{/if}
									<span class="w-full truncate">
										{shownAction !== null ? lineageActionLabel(shownAction) : t('app.gen.result')}
									</span>
								</div>
							</div>
						</div>
					{/if}
					{#if shown.assets.length > 0}
						<div class="relative min-h-0 flex-1">
							<a
								href={shown.assets[0].url}
								target="_blank"
								rel="noopener"
								class="block h-full"
								title={t('app.gen.open_full')}
							>
								<img
									src={shown.assets[0].url}
									alt={shown.params.prompt ?? t('app.gen.result')}
									class="h-full w-full rounded-lg object-contain"
								/>
							</a>
							<Button
								href={shown.assets[0].download_url}
								variant="secondary"
								size="sm"
								class="absolute top-2 right-2"
							>
								<DownloadIcon />
								{t('app.gen.download')}
							</Button>
						</div>
					{:else}
						<div
							class="border-border text-muted-foreground grid min-h-0 flex-1 place-items-center rounded-lg border border-dashed text-sm"
						>
							{t('app.lineage.missing')}
						</div>
					{/if}
					<p class="text-muted-foreground min-w-0 truncate text-center text-xs">
						{#if shown.params.prompt}
							{shown.params.prompt}
						{:else}
							{shown.model_id}{shownFactor != null ? ` · x${shownFactor}` : ''}
							{#if shown.assets.length > 0}
								· {shown.assets[0].width}x{shown.assets[0].height}
							{/if}
						{/if}
						{#if shown.gpu_ms != null}
							<span class="text-foreground/70">
								· {t('app.gen.gpu_time')} {formatMs(shown.gpu_ms)}</span
							>
						{/if}
					</p>
					{#if lineage !== null}
						<div class="shrink-0">
							<p class="text-muted-foreground mb-1 text-[0.65rem] font-medium uppercase">
								{t('app.lineage.derivatives')}
							</p>
							{#if lineage.children.length > 0}
								<div class="no-scrollbar flex min-w-0 gap-2 overflow-x-auto">
									{#each lineage.children as entry (entry.asset_id)}
										<button
											type="button"
											class="border-border bg-muted/30 hover:bg-muted flex w-20 shrink-0 flex-col items-center gap-1 rounded-md border p-1 text-[0.6rem]"
											title={lineageActionLabel(entry.action)}
											onclick={() => selectLineage(entry)}
										>
											{#if entry.thumbnail_url !== null}
												<img
													src={entry.thumbnail_url}
													alt={lineageActionLabel(entry.action)}
													class="size-12 rounded object-cover"
												/>
											{:else}
												<span
													class="border-border text-muted-foreground grid size-12 place-items-center rounded border border-dashed px-1 text-center leading-tight"
												>
													{t('app.lineage.missing')}
												</span>
											{/if}
											<span class="w-full truncate">{lineageActionLabel(entry.action)}</span>
										</button>
									{/each}
								</div>
							{:else}
								<p class="text-muted-foreground text-xs">{t('app.lineage.no_derivatives')}</p>
							{/if}
						</div>
					{/if}
				{:else}
					<div
						class="text-foreground/55 grid h-full place-items-center px-6 text-center text-xs tracking-[0.14em] uppercase"
					>
						{t('app.gen.result_hint')}
					</div>
				{/if}
			</Card.Content>
		</Card.Root>
		<div class="min-w-0 shrink-0">
			<HistoryStrip />
		</div>
	</div>
</div>
