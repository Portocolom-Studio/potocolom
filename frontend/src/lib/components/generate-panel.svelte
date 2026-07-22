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
		valueToNorm
	} from '$lib/model-params';
	import { formatMs } from '$lib/benchmark';
	import { estimateGpuMs, estimateUpscaleGpuMs } from '$lib/gpu-estimate';
	import {
		defaultUpscaleModelId,
		filterDiffusionModels,
		filterUpscaleModels,
		generationById,
		isStarred,
		loadHistory,
		pollWhileWorking,
		studio,
		toggleStarred,
		UPSCALE_FAST_ID,
		UPSCALE_QUALITY_ID,
		type Model
	} from '$lib/studio.svelte';
	import HistoryStrip from '$lib/components/history-strip.svelte';

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

	let panelMode = $state<'generate' | 'upscale'>('generate');
	let stepsNorm = $state(0);
	let guidanceNorm = $state(0);
	let sizeIndex = $state(0);
	let sizeContext = $state({ modelId: '', optionCount: 0 });
	let count = $state('1');
	let normsReady = $state(false);
	let seed = $state('');
	let sourceAssetId = $state<string | null>(null);
	let upscaleFactor = $state(2);
	// null = follow defaultUpscaleModelId (fast when present).
	let upscaleTierChoice = $state<string | null>(null);
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

	// Diffusion models drive the generate form; upscalers are chosen in the
	// Upscale panel mode (fast default, quality optional).
	const diffusionModels = $derived(filterDiffusionModels(studio.models));
	const upscaleModels = $derived(filterUpscaleModels(studio.models));
	const hasUpscaleFast = $derived(upscaleModels.some((model) => model.id === UPSCALE_FAST_ID));
	const hasUpscaleQuality = $derived(
		upscaleModels.some((model) => model.id === UPSCALE_QUALITY_ID)
	);
	const upscaleModelId = $derived(
		upscaleTierChoice !== null && upscaleModels.some((model) => model.id === upscaleTierChoice)
			? upscaleTierChoice
			: defaultUpscaleModelId(studio.models)
	);
	const upscaleModel = $derived(upscaleModels.find((model) => model.id === upscaleModelId));
	const selectedModel = $derived(studio.models.find((m) => m.id === studio.modelId));
	const canEdit = $derived(
		shown !== null &&
			shown.assets.length > 0 &&
			(selectedModel?.capabilities.includes('image_to_image') ?? false)
	);
	const canUpscale = $derived(
		shown !== null && shown.assets.length > 0 && shown.state === 'succeeded' && upscaleModel != null
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
	const upscaleSource = $derived(
		shown !== null && shown.assets.length > 0
			? { width: shown.assets[0].width, height: shown.assets[0].height }
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
			? `${upscaleModel.name} · x${upscaleFactor}` +
					(upscaleSource != null
						? ` · ${upscaleSource.width}x${upscaleSource.height}` +
							(upscaleOutLabel != null ? ` -> ${upscaleOutLabel}` : '')
						: '') +
					(upscaleEstimateLabel != null ? ` · ${upscaleEstimateLabel}` : '')
			: null
	);
	const shownFactor = $derived(
		typeof shown?.params.factor === 'number' ? shown.params.factor : null
	);
	const panelTitle = $derived(
		panelMode === 'generate' ? t('app.gen.title') : t('app.upscale.title')
	);
	const panelSub = $derived(panelMode === 'generate' ? t('app.gen.sub') : t('app.upscale.sub'));

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

	async function upscaleShown(): Promise<void> {
		if (!canUpscale || shown === null || upscaleModel == null) return;
		// Capture before clearing the selection: `shown` is derived from
		// selectedId, so reading it afterwards would target the newest
		// generation instead of the one on screen.
		const sourceId = shown.assets[0].id;
		const modelId = upscaleModel.id;
		errorText = '';
		studio.selectedId = null;
		const response = await fetch('/api/v1/generations', {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({
				model_id: modelId,
				params: { factor: upscaleFactor },
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
	}

	function onSizeChange(value: string): void {
		const index = sizeOptions.indexOf(Number(value));
		if (index >= 0) sizeIndex = index;
	}

	function onPanelModeChange(value: string): void {
		if (value === 'generate' || value === 'upscale') panelMode = value;
	}

	function onUpscaleFactorChange(value: string): void {
		const factor = Number(value);
		if (factor === 2 || factor === 4) upscaleFactor = factor;
	}

	function onUpscaleTierChange(value: string): void {
		if (value === UPSCALE_FAST_ID || value === UPSCALE_QUALITY_ID) {
			if (upscaleModels.some((model) => model.id === value)) upscaleTierChoice = value;
		}
	}

	function modelOptionLabel(model: Model | undefined): string {
		if (!model) return t('app.gen.model');
		return model.estimated_gpu_ms_default != null
			? `${model.name} (~${formatMs(model.estimated_gpu_ms_default)})`
			: model.name;
	}
</script>

<div class="grid h-full min-h-0 gap-4 lg:grid-cols-[minmax(300px,380px)_1fr]">
	<Card.Root class="no-scrollbar flex min-h-0 flex-col overflow-y-auto">
		<Card.Header class="gap-3">
			<ToggleGroup.Root
				type="single"
				variant="outline"
				spacing={0}
				class="flex w-full"
				value={panelMode}
				onValueChange={(value) => value && onPanelModeChange(value)}
			>
				<ToggleGroup.Item value="generate" class={`min-w-0 flex-1 text-xs ${toggleOnClass}`}>
					{t('app.gen.mode_generate')}
				</ToggleGroup.Item>
				<ToggleGroup.Item value="upscale" class={`min-w-0 flex-1 text-xs ${toggleOnClass}`}>
					{t('app.gen.mode_upscale')}
				</ToggleGroup.Item>
			</ToggleGroup.Root>
			<div class="flex flex-col gap-1.5">
				<Card.Title>{panelTitle}</Card.Title>
				<Card.Description>{panelSub}</Card.Description>
			</div>
		</Card.Header>
		<Card.Content class="flex min-h-0 flex-1 flex-col">
			{#if panelMode === 'generate'}
				{#if diffusionModels.length === 0}
					<p class="text-muted-foreground text-sm leading-relaxed">{t('app.gen.no_models')}</p>
				{:else}
					<form class="flex min-h-0 flex-1 flex-col gap-4" onsubmit={generate}>
						<div class="flex flex-col gap-2">
							<Label for="gen-model">{t('app.gen.model')}</Label>
							<Select.Root type="single" bind:value={studio.modelId}>
								<Select.Trigger id="gen-model" class="w-full" size="sm">
									{modelOptionLabel(
										diffusionModels.find((model) => model.id === studio.modelId) ??
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
				<p class="text-muted-foreground text-sm leading-relaxed">{t('app.upscale.no_models')}</p>
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
						<Label>{t('app.gen.upscaler')}</Label>
						{#if hasUpscaleFast || hasUpscaleQuality}
							<ToggleGroup.Root
								type="single"
								variant="outline"
								spacing={0}
								class="flex w-full"
								value={upscaleModelId}
								onValueChange={(value) => value && onUpscaleTierChange(value)}
							>
								<ToggleGroup.Item
									value={UPSCALE_FAST_ID}
									class={`min-w-0 flex-1 text-xs ${toggleOnClass}`}
									disabled={!hasUpscaleFast}
								>
									{t('app.gen.upscale_fast')}
								</ToggleGroup.Item>
								<ToggleGroup.Item
									value={UPSCALE_QUALITY_ID}
									class={`min-w-0 flex-1 text-xs ${toggleOnClass}`}
									disabled={!hasUpscaleQuality}
								>
									{t('app.gen.upscale_quality')}
								</ToggleGroup.Item>
							</ToggleGroup.Root>
						{:else}
							<p class="text-sm">{upscaleModel?.name ?? upscaleModelId}</p>
						{/if}
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
						{#if shown.params.prompt}
							{shown.params.prompt}
						{:else}
							{shown.model_id}{shownFactor != null ? ` · x${shownFactor}` : ''}
							· {shown.assets[0].width}x{shown.assets[0].height}
						{/if}
						{#if shown.gpu_ms != null}
							<span class="text-foreground/70">
								· {t('app.gen.gpu_time')} {formatMs(shown.gpu_ms)}</span
							>
						{/if}
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
		<div class="min-w-0 shrink-0">
			<HistoryStrip />
		</div>
	</div>
</div>
