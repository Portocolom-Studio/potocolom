<script lang="ts">
	import ChevronDownIcon from '@lucide/svelte/icons/chevron-down';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import ClipboardPasteIcon from '@lucide/svelte/icons/clipboard-paste';
	import DownloadIcon from '@lucide/svelte/icons/download';
	import HistoryIcon from '@lucide/svelte/icons/history';
	import LayersIcon from '@lucide/svelte/icons/layers';
	import Maximize2Icon from '@lucide/svelte/icons/maximize-2';
	import MinusIcon from '@lucide/svelte/icons/minus';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import SparklesIcon from '@lucide/svelte/icons/sparkles';
	import StarIcon from '@lucide/svelte/icons/star';
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Collapsible from '$lib/components/ui/collapsible';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import * as Select from '$lib/components/ui/select';
	import * as ToggleGroup from '$lib/components/ui/toggle-group';
	import { useStudioHeader } from '$lib/studio-chrome.svelte';
	import ParamSliderField from '$lib/components/param-slider-field.svelte';
	import {
		defaultSizeIndex,
		enumIndexToNorm,
		formatParamValue,
		guidanceSpec,
		modelProperty,
		normToEnumIndex,
		normToValue,
		sizeOptions as modelSizeOptions,
		stepsSpec,
		trackSteps,
		strengthSpec,
		valueToNorm
	} from '$lib/model-params';
	import { countError, seedError } from '$lib/generate-validation';
	import { apiFetch } from '$lib/api';
	import { shortcutLabel } from '$lib/platform.svelte';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import { toggleStarWithUndo } from '$lib/studio-toasts';
	import { revealOnLoad } from '$lib/image-reveal';
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
		recentPrompts,
		selectGeneration,
		studio,
		toggleStarred,
		type GenerationLineage,
		type LineageEntry,
		type Model
	} from '$lib/studio.svelte';
	import HistoryStrip from '$lib/components/history-strip.svelte';
	import { runExclusive, type SubmitLock } from '$lib/submit-once';

	let { mode }: { mode: 'generate' | 'image_to_image' | 'upscale' } = $props();

	// The field look of the studio's native controls (the prompt textarea).
	const fieldClass =
		'bg-field border-line-strong placeholder:text-dim focus-visible:ring-ring/60 w-full min-w-0 ' +
		'rounded-md border px-2.5 py-2 font-sans text-[13px] leading-normal outline-none focus-visible:ring-2 ' +
		'disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50';
	// A segmented control: the chosen segment lifts off the track.
	const segmentRootClass =
		'bg-field border-line-strong flex w-full gap-0.5 rounded-[7px] border p-0.5';
	const segmentItemClass =
		'text-muted-foreground h-[30px] min-w-0 flex-1 rounded-[5px]! border-0 text-xs font-medium tabular-nums ' +
		'hover:bg-transparent hover:text-foreground data-[state=on]:bg-hover data-[state=on]:text-foreground ' +
		'data-[state=on]:ring-line-strong data-[state=on]:ring-1 data-[state=on]:ring-inset';

	let stepsNorm = $state(0);
	let guidanceNorm = $state(0);
	let strengthNorm = $state(0);
	let sizeIndex = $state(0);
	let sizeContext = $state({ modelId: '', optionCount: 0 });
	let count = $state('1');
	let normsReady = $state(false);
	let seed = $state<number | null>(null);
	let sourceAssetId = $state<string | null>(null);
	let branchParams = $state<Record<string, unknown>>({});
	// Which mode raised the branch that set the two above. One panel instance
	// serves all three modes - the sidebar only swaps the prop - so without
	// this the source asset and params from a canvas branch outlive the mode
	// they belong to: Upscale would submit an asset it is not showing, and the
	// params would ride along into every later job of the session.
	let branchMode = $state<'generate' | 'image_to_image' | 'upscale' | null>(null);
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
	// Jobs still queue server side once the POST returns. The form itself
	// refuses a second click until that POST finishes (issue #455).
	const submitLock: SubmitLock = $state({ busy: false });
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
	// The generate form runs with novalidate, so these two are the only gate
	// on Count and Seed; the submit clamp keeps 1..8 for the count.
	const COUNT_MIN = 1;
	const COUNT_MAX = 8;
	const countProblem = $derived(countError(count, COUNT_MIN, COUNT_MAX));
	const seedProblem = $derived(seedError(seed, modelProperty(selectedModel, 'seed') ?? {}));
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
		seed = null;
		branchParams = { ...prefill.params };
		delete branchParams.seed;
		branchMode = mode;
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

	// Leaving the mode a branch was raised for ends that branch. A source the
	// user picked here by hand leaves branchMode null and is left alone.
	$effect(() => {
		if (branchMode === null || branchMode === mode) return;
		sourceAssetId = null;
		branchParams = {};
		branchMode = null;
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
		await runExclusive(submitLock, async () => {
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
				if (typeof seed === 'number' && Number.isFinite(seed)) params.seed = seed + index;
				const response = await apiFetch('/api/v1/generations', {
					method: 'POST',
					headers: { 'content-type': 'application/json' },
					body: JSON.stringify({
						model_id: activeModelId,
						params,
						...(mode === 'image_to_image' && sourceAssetId
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
		});
	}

	async function upscaleShown(): Promise<void> {
		if (!canUpscale || upscaleSourceAsset === null || upscaleModel == null) return;
		// Capture before the request: `shown` is derived from selectedId, so
		// reading it after clearing the selection would target the newest
		// generation instead of the one on screen.
		const sourceId = upscaleSourceAsset.id;
		const modelId = upscaleModel.id;
		await runExclusive(submitLock, async () => {
			errorText = '';
			studio.selectedId = null;
			const response = await apiFetch('/api/v1/generations', {
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
		});
	}

	function insertPrompt(): void {
		if (shownPrompt !== '') studio.prompt = shownPrompt;
	}

	function starShown(): void {
		if (shown === null) return;
		const id = shown.id;
		void toggleStarWithUndo(id, () => toggleStarred(id));
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

	const jobCount = $derived(Math.min(Math.max(Number(count) || 1, 1), 8));
	const totalEstimateLabel = $derived(
		gpuEstimateMs != null ? `~${formatMs(gpuEstimateMs * jobCount)}` : null
	);
	const tokenLabel = $derived(
		promptWindow > 0
			? t('app.realtime_canvas.tokens_of')
					.replace('{tokens}', String(promptTokens))
					.replace('{limit}', String(promptWindow))
			: t('app.realtime_canvas.tokens').replace('{tokens}', String(promptTokens))
	);
	const prompts = $derived(recentPrompts());
	const shownModel = $derived(
		shown === null ? undefined : studio.models.find((model) => model.id === shown.model_id)
	);
	const shownSize = $derived(
		shown !== null && shown.assets.length > 0
			? `${shown.assets[0].width} × ${shown.assets[0].height}`
			: null
	);
	const shownSeed = $derived(typeof shown?.params.seed === 'number' ? shown.params.seed : null);
	const shownSteps = $derived(typeof shown?.params.steps === 'number' ? shown.params.steps : null);
	const shownGuidance = $derived(
		typeof shown?.params.guidance === 'number' ? shown.params.guidance : null
	);

	function modelInitial(model: Model | undefined): string {
		return model?.name.trim().charAt(0).toUpperCase() ?? '-';
	}

	function modelMeta(model: Model | undefined): string {
		if (!model) return '';
		const parts: string[] = [];
		if (model.estimated_gpu_ms_default != null)
			parts.push(`~${formatMs(model.estimated_gpu_ms_default)} GPU`);
		if (model.min_vram_gb > 0) parts.push(`${model.min_vram_gb} GB`);
		return parts.join(' · ');
	}

	function stepCount(delta: number): void {
		count = String(Math.min(COUNT_MAX, Math.max(COUNT_MIN, (Number(count) || COUNT_MIN) + delta)));
	}

	/** Cmd or Ctrl and Enter submits the form from the prompt. */
	// requestSubmit() fires even while the submit button is disabled, so the
	// shortcut checks the same conditions as the button.
	const canGenerate = $derived(
		!submitLock.busy &&
			studio.prompt.trim() !== '' &&
			countProblem === null &&
			seedProblem === null &&
			!(mode === 'image_to_image' && sourceAssetId === null)
	);

	function onPromptKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Enter' || event.isComposing || !(event.metaKey || event.ctrlKey)) return;
		event.preventDefault();
		if (canGenerate) (event.currentTarget as HTMLTextAreaElement).form?.requestSubmit();
	}

	const MODES = [
		{ view: 'generate', label: 'app.service.generate' },
		{ view: 'image_to_image', label: 'app.service.image_to_image' },
		{ view: 'upscale', label: 'app.service.upscale' }
	] as const;

	useStudioHeader(() => ({ actions: modeSwitch }));
</script>

{#snippet modeSwitch()}
	<div
		class="bg-field border-line-strong hidden gap-0.5 rounded-[7px] border p-0.5 md:flex"
		role="group"
		aria-label={panelTitle}
	>
		{#each MODES as option (option.view)}
			<button
				type="button"
				class={'flex h-[26px] min-w-28 items-center justify-center rounded-[5px] px-3 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
					(mode === option.view
						? 'bg-hover text-foreground ring-line-strong shadow-[0_1px_2px_rgba(0,0,0,0.4)] ring-1 ring-inset'
						: 'text-muted-foreground hover:text-foreground')}
				aria-pressed={mode === option.view}
				onclick={() => openService(option.view)}
			>
				{t(option.label)}
			</button>
		{/each}
	</div>
{/snippet}

{#snippet modelCard(model: Model | undefined)}
	<span
		class="bg-raised ring-line-strong grid size-7 shrink-0 place-items-center rounded-md text-xs font-semibold ring-1 ring-inset"
		aria-hidden="true">{modelInitial(model)}</span
	>
	<span class="grid min-w-0 flex-1 text-start leading-tight">
		<span class="text-foreground truncate font-medium">{model?.name ?? t('app.gen.model')}</span>
		{#if modelMeta(model) !== ''}
			<span class="text-dim truncate text-[11px] tabular-nums">{modelMeta(model)}</span>
		{/if}
	</span>
{/snippet}

{#snippet progress()}
	{#if working > 0}
		<div class="mt-3">
			<div class="text-dim flex items-center justify-between text-[11.5px] tabular-nums">
				<span role="status" title={t('app.gen.working_suffix')}
					>{working} {t('app.gen.working_short')}</span
				>
				{#if runningProgress !== null}
					<span aria-hidden="true">{Math.round(runningProgress * 100)}%</span>
				{/if}
			</div>
			{#if runningProgress !== null}
				<div class="bg-line-strong mt-1.5 h-0.5 overflow-hidden rounded-full" aria-hidden="true">
					<div
						class="bg-brand h-full rounded-full"
						style:width={`${Math.round(runningProgress * 100)}%`}
					></div>
				</div>
			{/if}
		</div>
	{/if}
	{#if errorText !== ''}
		<p role="alert" class="text-destructive mt-2 text-[13px] leading-relaxed">{errorText}</p>
	{/if}
{/snippet}

<div class="flex h-full min-h-0 flex-col overflow-y-auto lg:flex-row lg:overflow-hidden">
	<aside
		class="bg-panel border-border flex min-h-0 flex-none flex-col border-b lg:w-[340px] lg:border-e lg:border-b-0"
		aria-label={panelTitle}
	>
		<h1 class="sr-only">{panelTitle}</h1>
		<p class="sr-only">{panelSub}</p>
		{#if !studio.modelsLoaded}
			<!-- The parameter column's shape while the models load. -->
			<div role="status" class="flex flex-col gap-4 p-4">
				<span class="sr-only">{t('app.models.loading')}</span>
				<div class="flex flex-col gap-2" aria-hidden="true">
					<Skeleton class="h-3 w-12 rounded" />
					<Skeleton class="h-[46px] rounded-md" />
				</div>
				<div class="flex flex-col gap-2" aria-hidden="true">
					<Skeleton class="h-3 w-14 rounded" />
					<Skeleton class="h-[136px] rounded-md" />
				</div>
				{#each [0, 1] as index (index)}
					<div class="flex flex-col gap-2" aria-hidden="true">
						<Skeleton class="h-3 w-20 rounded" />
						<Skeleton class="h-1 rounded-full" />
					</div>
				{/each}
				<Skeleton class="mt-2 h-9 rounded-md" aria-hidden="true" />
			</div>
		{:else if mode !== 'upscale'}
			{#if diffusionModels.length === 0}
				<p class="text-muted-foreground p-4 text-[13px] leading-relaxed">
					{compatibleModelsRegistered ? t('app.models.none_available') : t('app.gen.no_models')}
				</p>
			{:else}
				<form class="flex min-h-0 flex-1 flex-col" novalidate onsubmit={generate}>
					<div class="no-scrollbar min-h-0 flex-1 overflow-y-auto">
						<div class="border-border border-b p-4">
							<Label for="gen-model" class="text-muted-foreground mb-2 text-xs font-medium"
								>{t('app.gen.model')}</Label
							>
							<Select.Root
								type="single"
								value={activeModelId}
								onValueChange={(value) => value && onModelChange(value)}
							>
								<Select.Trigger
									id="gen-model"
									class="bg-field border-line-strong h-auto! w-full gap-2.5 px-2.5 py-2 font-sans"
								>
									{@render modelCard(
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
								<p class="text-dim mt-2 text-xs">{selectedModel.requires_attribution}</p>
							{/if}
						</div>
						{#if mode === 'image_to_image'}
							<div class="border-border border-b p-4">
								<p class="text-muted-foreground mb-2 text-xs font-medium">
									{t('app.image_to_image.source')}
								</p>
								<div
									class="border-line-strong flex min-h-16 items-center gap-3 rounded-md border border-dashed p-2.5"
								>
									{#if sourceAsset !== null}
										<img
											src={sourceAsset.thumbnail_url ?? sourceAsset.url}
											alt={t('app.image_to_image.source')}
											class="size-12 rounded-md object-cover"
											width="48"
											height="48"
										/>
										<p class="min-w-0 flex-1 truncate text-[13px]">
											{t('app.image_to_image.ready')}
										</p>
									{:else}
										<p class="text-muted-foreground flex-1 text-[13px]">
											{t('app.image_to_image.source_empty')}
										</p>
									{/if}
								</div>
								<div class="mt-2 flex gap-2">
									<Button
										type="button"
										variant="outline"
										size="sm"
										class="flex-1"
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
						<div class="border-border border-b p-4">
							<div class="mb-2 flex items-center justify-between gap-2">
								<Label for="gen-prompt" class="text-muted-foreground text-xs font-medium"
									>{t('app.gen.prompt')}</Label
								>
								<span class="text-dim text-[11px] tabular-nums">{tokenLabel}</span>
							</div>
							<textarea
								id="gen-prompt"
								name="prompt"
								autocomplete="off"
								class={fieldClass + ' no-scrollbar h-[136px] resize-none overflow-y-auto'}
								placeholder={t('app.gen.prompt_placeholder')}
								aria-describedby={promptTokenNotice ? 'gen-prompt-window' : undefined}
								aria-keyshortcuts="Meta+Enter Control+Enter"
								onkeydown={onPromptKeydown}
								bind:value={studio.prompt}></textarea>
							{#if promptTokenNotice}
								<!-- Described by the field rather than announced from a live region:
								the count changes on every keystroke, which a polite region would
								read out again and again while the user is still typing. -->
								<p
									id="gen-prompt-window"
									class="text-status-warning mt-1.5 text-xs leading-relaxed tabular-nums"
								>
									{promptTokenNotice}
								</p>
							{/if}
							<div class="mt-2 flex flex-wrap gap-1.5">
								<DropdownMenu.Root>
									<DropdownMenu.Trigger>
										{#snippet child({ props })}
											<button
												type="button"
												{...props}
												class="border-line-strong text-muted-foreground hover:text-foreground hover:bg-hover flex h-[22px] items-center gap-1.5 rounded-full border px-2 text-[11.5px] font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring"
											>
												<HistoryIcon class="size-3" aria-hidden="true" />
												{t('app.gen.prompt_history')}
											</button>
										{/snippet}
									</DropdownMenu.Trigger>
									<DropdownMenu.Content class="w-80 max-w-[calc(100vw-2rem)]" align="start">
										{#each prompts as recent (recent)}
											<DropdownMenu.Item title={recent} onclick={() => (studio.prompt = recent)}>
												<span class="line-clamp-2 text-[13px]">{recent}</span>
											</DropdownMenu.Item>
										{:else}
											<p class="text-muted-foreground px-2 py-1.5 text-[13px]">
												{t('app.shell.prompt_history_empty')}
											</p>
										{/each}
									</DropdownMenu.Content>
								</DropdownMenu.Root>
								<button
									type="button"
									class="border-line-strong text-muted-foreground hover:text-foreground hover:bg-hover flex h-[22px] items-center gap-1.5 rounded-full border px-2 text-[11.5px] font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
									disabled={shownPrompt === ''}
									onclick={insertPrompt}
								>
									<ClipboardPasteIcon class="size-3" aria-hidden="true" />
									{t('app.gen.insert_prompt')}
								</button>
							</div>
						</div>
						<div class="border-border border-b p-4">
							<Label id="gen-size-label" class="text-muted-foreground mb-2 text-xs font-medium"
								>{t('app.gen.size')}</Label
							>
							<ToggleGroup.Root
								type="single"
								aria-labelledby="gen-size-label"
								spacing={0}
								class={segmentRootClass}
								value={sizeKey}
								onValueChange={onSizeChange}
							>
								{#each sizeOptions as option (option)}
									<ToggleGroup.Item
										value={String(option)}
										aria-label={`${option} x ${option}`}
										class={segmentItemClass}
									>
										{option}²
									</ToggleGroup.Item>
								{/each}
							</ToggleGroup.Root>
							<div class="mt-3.5 grid grid-cols-2 gap-3">
								<div class="flex flex-col gap-2">
									<Label for="gen-count" class="text-muted-foreground text-xs font-medium"
										>{t('app.gen.count')}</Label
									>
									<div class="relative">
										<Input
											id="gen-count"
											name="count"
											autocomplete="off"
											inputmode="numeric"
											class="pe-14 tabular-nums [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
											type="number"
											min="1"
											max="8"
											aria-describedby={countProblem ? 'gen-count-error' : undefined}
											bind:value={count}
										/>
										<div class="absolute inset-y-0 end-1 flex items-center">
											<button
												type="button"
												class="text-dim hover:text-foreground grid size-6 place-items-center rounded outline-none focus-visible:ring-2 focus-visible:ring-ring"
												aria-label={t('app.gen.count_less')}
												onclick={() => stepCount(-1)}
												><MinusIcon class="size-3.5" aria-hidden="true" /></button
											>
											<button
												type="button"
												class="text-dim hover:text-foreground grid size-6 place-items-center rounded outline-none focus-visible:ring-2 focus-visible:ring-ring"
												aria-label={t('app.gen.count_more')}
												onclick={() => stepCount(1)}
												><PlusIcon class="size-3.5" aria-hidden="true" /></button
											>
										</div>
									</div>
									{#if countProblem}
										<p id="gen-count-error" class="text-destructive text-xs leading-relaxed">
											{t(countProblem)
												.replace('{min}', String(COUNT_MIN))
												.replace('{max}', String(COUNT_MAX))}
										</p>
									{/if}
								</div>
								<div class="flex flex-col gap-2">
									<Label for="gen-seed" class="text-muted-foreground text-xs font-medium"
										>{t('app.gen.seed')}</Label
									>
									<div class="relative">
										<Input
											id="gen-seed"
											name="seed"
											autocomplete="off"
											inputmode="numeric"
											class="pe-8 font-mono tabular-nums [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
											type="number"
											placeholder={t('app.gen.seed_random')}
											aria-describedby={seedProblem ? 'gen-seed-error' : undefined}
											bind:value={seed}
										/>
										<button
											type="button"
											class="text-dim hover:text-foreground absolute inset-y-0 end-1 my-auto grid size-6 place-items-center rounded outline-none focus-visible:ring-2 focus-visible:ring-ring"
											aria-label={t('app.gen.seed_clear')}
											title={t('app.gen.seed_clear')}
											onclick={() => (seed = null)}
											><RefreshCwIcon class="size-3.5" aria-hidden="true" /></button
										>
									</div>
									{#if seedProblem}
										<p id="gen-seed-error" class="text-destructive text-xs leading-relaxed">
											{t(seedProblem)}
										</p>
									{/if}
								</div>
							</div>
						</div>
						{#if normsReady}
							<Collapsible.Root open class="group/advanced p-4">
								<Collapsible.Trigger
									class="flex w-full items-center justify-between rounded text-xs font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring"
								>
									{t('app.gen.advanced')}
									<ChevronDownIcon
										class="text-dim size-3.5 transition-transform group-data-[state=closed]/advanced:-rotate-90"
										aria-hidden="true"
									/>
								</Collapsible.Trigger>
								<Collapsible.Content class="mt-3.5 flex flex-col gap-4">
									<ParamSliderField
										id="gen-steps"
										label={t('app.gen.steps')}
										bind:norm={stepsNorm}
										steps={trackSteps(stepsRange)}
										minLabel={formatParamValue(stepsRange.min, stepsRange)}
										maxLabel={formatParamValue(stepsRange.max, stepsRange)}
										valueLabel={formatParamValue(stepsValue, stepsRange)}
									/>
									<ParamSliderField
										id="gen-guidance"
										label={t('app.gen.guidance')}
										bind:norm={guidanceNorm}
										steps={trackSteps(guidanceRange)}
										minLabel={formatParamValue(guidanceRange.min, guidanceRange)}
										maxLabel={formatParamValue(guidanceRange.max, guidanceRange)}
										valueLabel={formatParamValue(guidanceValue, guidanceRange)}
									/>
									{#if mode === 'image_to_image'}
										<ParamSliderField
											id="gen-strength"
											label={t('app.image_to_image.strength')}
											bind:norm={strengthNorm}
											steps={trackSteps(strengthRange)}
											minLabel={formatParamValue(strengthRange.min, strengthRange)}
											maxLabel={formatParamValue(strengthRange.max, strengthRange)}
											valueLabel={formatParamValue(strengthValue, strengthRange)}
										/>
									{/if}
								</Collapsible.Content>
							</Collapsible.Root>
						{/if}
					</div>
					<div class="border-border border-t p-4">
						<Button
							type="submit"
							variant="brand"
							class="h-9 w-full tabular-nums"
							disabled={!canGenerate}
						>
							<SparklesIcon />
							{mode === 'image_to_image' ? t('app.image_to_image.generate') : t('app.gen.generate')}
							{jobCount > 1 ? jobCount : ''}
							{#if totalEstimateLabel != null}
								<span class="opacity-75">{totalEstimateLabel}</span>
							{/if}
							<kbd
								class="ms-1 rounded border border-white/30 px-1 font-sans text-[11px] leading-[15px] text-[#dfe6ff]"
								>{shortcutLabel(['mod', 'enter'])}</kbd
							>
						</Button>
						{@render progress()}
					</div>
				</form>
			{/if}
		{:else if upscaleModels.length === 0}
			<p class="text-muted-foreground p-4 text-[13px] leading-relaxed">
				{compatibleModelsRegistered ? t('app.models.none_available') : t('app.upscale.no_models')}
			</p>
		{:else if !canUpscale}
			<p class="text-muted-foreground p-4 text-[13px] leading-relaxed">
				{t('app.upscale.need_image')}
			</p>
		{:else}
			<div class="flex min-h-0 flex-1 flex-col">
				<div class="no-scrollbar min-h-0 flex-1 overflow-y-auto">
					<div class="border-border border-b p-4">
						<p class="text-muted-foreground mb-2 text-xs font-medium">{t('app.upscale.source')}</p>
						<div class="border-line-strong flex items-center gap-3 rounded-md border p-2.5">
							{#if upscaleSourceAsset !== null}
								<img
									src={upscaleSourceAsset.thumbnail_url ?? upscaleSourceAsset.url}
									alt=""
									class="size-12 rounded-md object-cover"
									width="48"
									height="48"
								/>
							{/if}
							<p class="text-muted-foreground min-w-0 flex-1 truncate text-[13px]">
								{shownPrompt !== '' ? shownPrompt : t('app.gen.result')}
							</p>
						</div>
					</div>
					<div class="border-border border-b p-4">
						<Label for="upscale-model" class="text-muted-foreground mb-2 text-xs font-medium"
							>{t('app.gen.upscaler')}</Label
						>
						<Select.Root
							type="single"
							value={upscaleModelId}
							onValueChange={(value) => value && onUpscaleModelChange(value)}
						>
							<Select.Trigger
								id="upscale-model"
								class="bg-field border-line-strong h-auto! w-full gap-2.5 px-2.5 py-2 font-sans"
							>
								{@render modelCard(upscaleModel)}
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
							<p class="text-dim mt-2 text-xs leading-relaxed tabular-nums">{upscaleChoiceLabel}</p>
						{/if}
					</div>
					<div class="p-4">
						<Label id="upscale-factor-label" class="text-muted-foreground mb-2 text-xs font-medium"
							>{t('app.upscale.factor')}</Label
						>
						<ToggleGroup.Root
							type="single"
							aria-labelledby="upscale-factor-label"
							spacing={0}
							class={segmentRootClass}
							value={String(upscaleFactor)}
							onValueChange={onUpscaleFactorChange}
						>
							<ToggleGroup.Item value="2" class={segmentItemClass}>
								{t('app.gen.upscale_x2')}
							</ToggleGroup.Item>
							<ToggleGroup.Item value="4" class={segmentItemClass}>
								{t('app.gen.upscale_x4')}
							</ToggleGroup.Item>
						</ToggleGroup.Root>
					</div>
				</div>
				<div class="border-border border-t p-4">
					<Button
						type="button"
						variant="brand"
						class="h-9 w-full tabular-nums"
						disabled={submitLock.busy || !canUpscale}
						onclick={upscaleShown}
					>
						<Maximize2Icon />
						{t('app.gen.upscale')}{upscaleEstimateLabel != null ? ` ${upscaleEstimateLabel}` : ''}
					</Button>
					{@render progress()}
				</div>
			</div>
		{/if}
	</aside>

	<!-- min-w-0: the thumbnail strip's intrinsic width must not widen the column -->
	<section class="flex min-h-0 min-w-0 flex-1 flex-col" aria-label={t('app.gen.result')}>
		<div
			class="border-border flex min-h-[50px] flex-wrap items-center gap-2 border-b px-4 py-2 lg:flex-nowrap"
		>
			{#if shown !== null && lineage !== null}
				<div
					class="no-scrollbar flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
					role="group"
					aria-label={t('app.lineage.ancestors')}
				>
					{#each lineage.ancestors as entry (entry.asset_id)}
						<button
							type="button"
							class="text-muted-foreground hover:text-foreground hover:bg-hover flex shrink-0 items-center gap-2 rounded-md py-1 ps-1 pe-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default"
							disabled={entry.job_id === null}
							title={lineageActionLabel(entry.action)}
							onclick={() => selectLineage(entry)}
						>
							{#if entry.thumbnail_url !== null}
								<img
									src={entry.thumbnail_url}
									alt=""
									class="size-8 rounded object-cover"
									width="32"
									height="32"
									loading="lazy"
								/>
							{:else}
								<span
									class="border-line-strong text-dim grid size-8 place-items-center rounded border border-dashed text-[9px]"
									>?</span
								>
							{/if}
							<span class="truncate">{lineageActionLabel(entry.action)}</span>
						</button>
						<ChevronRightIcon class="text-dim size-3 shrink-0" aria-hidden="true" />
					{/each}
					<div
						class="text-foreground flex shrink-0 items-center gap-2 rounded-md py-1 ps-1 pe-2 text-xs"
						aria-current="true"
					>
						{#if shownThumbnail !== null}
							<img
								src={shownThumbnail}
								alt=""
								class="ring-brand size-8 rounded object-cover ring-1"
								width="32"
								height="32"
								loading="lazy"
							/>
						{/if}
						<span class="truncate">
							{shownAction !== null ? lineageActionLabel(shownAction) : t('app.gen.result')}
							{shownFactor != null ? ` ×${shownFactor}` : ''}
						</span>
					</div>
				</div>
			{:else}
				<div class="flex-1"></div>
			{/if}
			{#if shown !== null}
				<div class="flex shrink-0 items-center gap-1.5">
					<Button
						type="button"
						variant="ghost"
						size="icon-sm"
						class={'hit-slop relative [--hit-slop-x:3px] ' +
							(shownStarred ? 'text-status-warning' : 'text-muted-foreground')}
						aria-pressed={shownStarred}
						aria-label={shownStarred ? t('app.gen.unstar') : t('app.gen.star')}
						title={shownStarred ? t('app.gen.unstar') : t('app.gen.star')}
						onclick={starShown}
					>
						<StarIcon class={shownStarred ? 'fill-current' : ''} />
					</Button>
					<Button
						type="button"
						variant="outline"
						size="sm"
						aria-pressed={sourceAssetId !== null && mode === 'image_to_image'}
						disabled={!canEdit}
						onclick={editShown}
					>
						<LayersIcon />
						{t('app.gen.use_as_source')}
					</Button>
					<Button
						type="button"
						variant="outline"
						size="sm"
						disabled={shown.assets.length === 0 ||
							shown.state !== 'succeeded' ||
							mode === 'upscale'}
						onclick={() => openService('upscale')}
					>
						<Maximize2Icon />
						{t('app.gen.upscale')}
					</Button>
					{#if shown.assets.length > 0}
						<Button href={shown.assets[0].download_url} variant="outline" size="sm">
							<DownloadIcon />
							{t('app.gen.download')}
						</Button>
					{/if}
				</div>
			{/if}
		</div>

		<div class="flex min-h-0 flex-1 flex-col lg:flex-row">
			{#if shown !== null}
				<div class="relative flex min-h-72 min-w-0 flex-1 items-center justify-center p-6 lg:p-10">
					{#if shown.assets.length > 0}
						{@const asset = shown.assets[0]}
						<!-- The thumbnail, blurred, holds the frame while the full image
						     loads, in the same grid cell so both take the same box. -->
						<a
							href={asset.url}
							target="_blank"
							rel="noopener"
							class="reveal-stack grid h-full max-h-full w-full grid-cols-1 grid-rows-[minmax(0,1fr)]"
							title={t('app.gen.open_full')}
						>
							{#if asset.thumbnail_url !== null && asset.thumbnail_url !== asset.url}
								<img
									src={asset.thumbnail_url}
									alt=""
									aria-hidden="true"
									class="img-placeholder col-start-1 row-start-1 mx-auto h-full max-h-full w-auto max-w-full rounded-[10px] object-contain"
									width={asset.width}
									height={asset.height}
								/>
							{/if}
							<img
								src={asset.url}
								alt={shown.params.prompt ?? t('app.gen.result')}
								class="img-reveal col-start-1 row-start-1 mx-auto h-full max-h-full w-auto max-w-full rounded-[10px] object-contain"
								width={asset.width}
								height={asset.height}
								{@attach revealOnLoad(asset.url)}
							/>
						</a>
					{:else}
						<div
							class="border-line-strong text-muted-foreground grid h-full min-h-64 w-full place-items-center rounded-[10px] border border-dashed text-[13px]"
						>
							{t('app.lineage.missing')}
						</div>
					{/if}
				</div>
				<dl
					class="border-border flex shrink-0 flex-col gap-3.5 border-t px-6 py-5 text-[13px] lg:w-[260px] lg:justify-center lg:border-t-0 lg:ps-0"
				>
					{#if shownPrompt !== ''}
						<div>
							<dt class="text-dim text-[11.5px]">{t('app.gen.prompt')}</dt>
							<dd class="mt-0.5 break-words">{shownPrompt}</dd>
						</div>
					{/if}
					<div>
						<dt class="text-dim text-[11.5px]">{t('app.gen.model')}</dt>
						<dd class="mt-0.5">{shownModel?.name ?? shown.model_id}</dd>
					</div>
					{#if shownSize !== null}
						<div>
							<dt class="text-dim text-[11.5px]">{t('app.gen.size')}</dt>
							<dd class="mt-0.5 tabular-nums">{shownSize}</dd>
						</div>
					{/if}
					{#if shownSeed !== null}
						<div>
							<dt class="text-dim text-[11.5px]">{t('app.gen.seed')}</dt>
							<dd class="mt-0.5 font-mono">{shownSeed}</dd>
						</div>
					{/if}
					{#if shownSteps !== null || shownGuidance !== null}
						<div>
							<dt class="text-dim text-[11.5px]">{t('app.gen.steps')} · {t('app.gen.guidance')}</dt>
							<dd class="mt-0.5 tabular-nums">
								{shownSteps ?? '-'} · {shownGuidance ?? '-'}
							</dd>
						</div>
					{/if}
					{#if shown.gpu_ms != null}
						<div>
							<dt class="text-dim text-[11.5px]">{t('app.gen.gpu_time')}</dt>
							<dd class="mt-0.5 tabular-nums">{formatMs(shown.gpu_ms)}</dd>
						</div>
					{/if}
					{#if lineage !== null}
						<div>
							<dt class="text-dim text-[11.5px]">{t('app.lineage.derivatives')}</dt>
							<dd class="mt-1">
								{#if lineage.children.length > 0}
									<div class="flex flex-wrap gap-1.5">
										{#each lineage.children as entry (entry.asset_id)}
											<button
												type="button"
												class="ring-line-strong hover:ring-foreground rounded ring-1 outline-none focus-visible:ring-2 focus-visible:ring-ring"
												title={lineageActionLabel(entry.action)}
												onclick={() => selectLineage(entry)}
											>
												{#if entry.thumbnail_url !== null}
													<img
														src={entry.thumbnail_url}
														alt={lineageActionLabel(entry.action)}
														class="size-10 rounded object-cover"
														width="40"
														height="40"
														loading="lazy"
													/>
												{:else}
													<span class="text-dim grid size-10 place-items-center text-[9px]"
														>{lineageActionLabel(entry.action)}</span
													>
												{/if}
											</button>
										{/each}
									</div>
								{:else}
									<span>{t('app.lineage.no_derivatives')}</span>
								{/if}
							</dd>
						</div>
					{/if}
				</dl>
			{:else if !studio.historyLoaded}
				<div
					role="status"
					class="relative flex min-h-72 min-w-0 flex-1 items-center justify-center p-6 lg:p-10"
				>
					<span class="sr-only">{t('app.gen.history_loading')}</span>
					<Skeleton
						class="aspect-square h-full max-h-full max-w-full rounded-[10px]"
						aria-hidden="true"
					/>
				</div>
			{:else}
				<div
					class="text-dim grid min-h-72 flex-1 place-items-center px-6 text-center text-xs tracking-[0.14em] uppercase"
				>
					{t('app.gen.result_hint')}
				</div>
			{/if}
		</div>
		<div class="border-border min-w-0 shrink-0 border-t px-4 py-3">
			<HistoryStrip />
		</div>
	</section>
</div>
