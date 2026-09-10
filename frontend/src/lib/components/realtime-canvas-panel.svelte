<script lang="ts">
	// The live realtime drawing surface (issue #3). One 512 by 512 bitmap, CSS
	// scaled for display, sent as complete WebP frames over the realtime
	// protocol. The session lifecycle lives in $lib/realtime-canvas; this panel
	// keeps the bitmap DOM and drawing controls.
	import { t } from '$lib/i18n.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { Slider } from '$lib/components/ui/slider';
	import { DrawingDocument, DRAWING_FILE_MAX_BYTES } from '$lib/drawing-document';
	import ParamSliderField from '$lib/components/param-slider-field.svelte';
	import {
		formatParamValue,
		normToValue,
		stepsSpec,
		trackSteps,
		structureStrengthSpec,
		valueToNorm
	} from '$lib/model-params';
	import { fallbackModelId, modelIsRemoved, studio, type Model } from '$lib/studio.svelte';
	import {
		createRealtimeCanvasSession,
		type ConnectionState,
		type RealtimeCanvasNotice
	} from '$lib/realtime-canvas';

	/** The wire dimensions. CSS scales the display without changing these. */
	const CANVAS_SIZE = 512;
	const INK = '#111827';
	const PAPER = '#ffffff';
	const BRUSH_COLORS = [
		{ value: '#111827', label: 'app.realtime_canvas.color_black' },
		{ value: '#dc2626', label: 'app.realtime_canvas.color_red' },
		{ value: '#ea580c', label: 'app.realtime_canvas.color_orange' },
		{ value: '#ca8a04', label: 'app.realtime_canvas.color_yellow' },
		{ value: '#16a34a', label: 'app.realtime_canvas.color_green' },
		{ value: '#2563eb', label: 'app.realtime_canvas.color_blue' },
		{ value: '#9333ea', label: 'app.realtime_canvas.color_purple' },
		{ value: '#db2777', label: 'app.realtime_canvas.color_pink' }
	] as const;

	let drawCanvas = $state<HTMLCanvasElement | undefined>();
	let outputCanvas = $state<HTMLCanvasElement | undefined>();
	let drawingDocument = $state<DrawingDocument | null>(null);
	let drawingFileInput = $state<HTMLInputElement | undefined>();

	/** A message is held as its key, not its text, so switching language
	 * retranslates it instead of leaving the previous locale on screen. */
	type NoticeKey = Parameters<typeof t>[0];
	const NOTICE_KEYS: Record<Exclude<RealtimeCanvasNotice, ''>, NoticeKey> = {
		encode_failed: 'app.realtime_canvas.encode_failed',
		decode_failed: 'app.realtime_canvas.decode_failed',
		socket_error: 'app.realtime_canvas.socket_error',
		refused_protocol: 'app.realtime_canvas.refused_protocol',
		refused_version: 'app.realtime_canvas.refused_version',
		refused_capacity: 'app.realtime_canvas.refused_capacity',
		refused_model: 'app.realtime_canvas.refused_model'
	};

	let prompt = $state('');
	let connection = $state<ConnectionState>('idle');
	let notice = $state<NoticeKey | ''>('');
	let drawingNotice = $state<NoticeKey | ''>('');
	let sentFrames = $state(0);
	let renderedFrames = $state(0);
	// The params the API last confirmed for this session, from the open message
	// and from params_updated. The Update button and the slider debounce compare
	// against these rather than against the inputs, so a rejected update leaves
	// the control dirty. They are what the API holds, not proof a worker ran
	// them: a confirmation can arrive while a reassignment is in flight.
	let appliedPrompt = $state('');
	let appliedStructure = $state(0);
	let appliedSteps = $state(0);

	let blank = $state(true);
	let strokePointer: number | null = null;
	let canUndo = $state(false);
	let canRedo = $state(false);
	let brushSize = $state(6);
	let selectedColor = $state(INK);
	// Slider updates are debounced so a drag does not send one message per
	// pixel: the timer is re-armed on every movement and fires once the slider
	// has settled, mirroring how armCapture/stopTimer time the capture loop.
	const SLIDER_UPDATE_MS = 300;
	let structureTimer: ReturnType<typeof setTimeout> | null = null;
	let stepsTimer: ReturnType<typeof setTimeout> | null = null;

	let tool = $state<'draw' | 'erase'>('draw');
	let openingDrawing = $state(false);
	let fileRequest = 0;

	// Only a model advertising the realtime capability can take canvas frames,
	// and only one the user has not removed in Models: that screen promises a
	// removed model disappears from the service pickers, and every other picker
	// filters the same way.
	const realtimeModels = $derived(
		studio.models.filter(
			(model) => model.capabilities.includes('realtime') && !modelIsRemoved(model.id)
		)
	);
	// The chosen model. Not derived: the picker writes it. An effect keeps a
	// removal or a reorder from leaving it pointing at a model that is not (or
	// no longer is) a realtime one, while still letting the user pick freely
	// among the current entries.
	let modelId = $state('');
	const selectedModel = $derived(realtimeModels.find((model) => model.id === modelId));
	const stepsRange = $derived(stepsSpec(selectedModel));
	const structureRange = $derived(structureStrengthSpec(selectedModel));
	// Norm positions for the sliders. The defaults belong to the model, so a
	// model change re-seeds them rather than carrying old positions into a range
	// that does not share them.
	let stepsNorm = $state(0);
	let structureNorm = $state(0);
	let normForModelId = $state('');
	const stepsValue = $derived(normToValue(stepsNorm, stepsRange));
	const structureValue = $derived(normToValue(structureNorm, structureRange));
	const connected = $derived(connection === 'active' || connection === 'resuming');
	const busy = $derived(connection === 'connecting' || connected);
	const canConnect = $derived(!busy && modelId !== '' && prompt.trim() !== '');
	// The prompt differs from the last one the API confirmed; whitespace around
	// it does not count, because openMessage and updateParamsMessage both trim.
	const promptDirty = $derived(connected && prompt.trim() !== appliedPrompt);

	const realtimeSession = createRealtimeCanvasSession({
		getDrawCanvas: () => drawCanvas,
		getOutputCanvas: () => outputCanvas,
		isCanvasBlank: () => drawingDocument?.isBlank ?? true,
		onState: (state) => {
			connection = state;
		},
		onNotice: (key: RealtimeCanvasNotice) => {
			notice = key === '' ? '' : NOTICE_KEYS[key];
		},
		onCounters: (sent, rendered) => {
			sentFrames = sent;
			renderedFrames = rendered;
		},
		onAppliedParams: (params) => {
			if (params.prompt !== undefined) appliedPrompt = params.prompt;
			if (params.structure_strength !== undefined) appliedStructure = params.structure_strength;
			if (params.steps !== undefined) appliedSteps = params.steps;
		}
	});

	const STATUS_KEYS = {
		idle: 'app.realtime_canvas.status_idle',
		connecting: 'app.realtime_canvas.status_connecting',
		queued: 'app.realtime_canvas.status_queued',
		active: 'app.realtime_canvas.status_active',
		resuming: 'app.realtime_canvas.status_resuming',
		interrupted: 'app.realtime_canvas.status_interrupted',
		failed: 'app.realtime_canvas.status_failed'
	} as const;
	const statusLabel = $derived(t(STATUS_KEYS[connection]));

	$effect(() => {
		// Fall back to the declared default, else the first realtime model, when
		// the chosen one is gone, and to no model at all when the list is empty.
		// The picker can then write a modelId that survives until the list
		// changes under it.
		if (realtimeModels.length === 0) {
			modelId = '';
			return;
		}
		if (!realtimeModels.some((model) => model.id === modelId)) {
			modelId = fallbackModelId(realtimeModels);
		}
	});

	$effect(() => {
		// Re-seed the slider positions for the model the picker now shows.
		if (!selectedModel || normForModelId === modelId) return;
		stepsNorm = valueToNorm(stepsRange.default, stepsRange);
		structureNorm = valueToNorm(structureRange.default, structureRange);
		normForModelId = modelId;
	});

	// Each effect clears the slider's timer on any of its dependencies
	// changing and re-arms it, so a drag that keeps moving never sends: the
	// update goes out once the value has held still for SLIDER_UPDATE_MS.
	// While the session is not connected nothing is armed, and a value the API
	// has already confirmed never sends, so reconnects and confirmations stay
	// quiet.
	$effect(() => {
		if (structureTimer !== null) {
			clearTimeout(structureTimer);
			structureTimer = null;
		}
		if (!connected || structureValue === appliedStructure) return;
		structureTimer = setTimeout(() => {
			structureTimer = null;
			realtimeSession.updateParams({ structure_strength: structureValue });
		}, SLIDER_UPDATE_MS);
	});

	$effect(() => {
		if (stepsTimer !== null) {
			clearTimeout(stepsTimer);
			stepsTimer = null;
		}
		if (!connected || stepsValue === appliedSteps) return;
		stepsTimer = setTimeout(() => {
			stepsTimer = null;
			realtimeSession.updateParams({ steps: stepsValue });
		}, SLIDER_UPDATE_MS);
	});

	$effect(() => {
		if (!drawCanvas) return;
		const owner = new DrawingDocument(drawCanvas);
		drawingDocument = owner;
		blank = owner.isBlank;
		return () => {
			fileRequest += 1;
			owner.destroy();
			if (drawingDocument === owner) drawingDocument = null;
		};
	});

	// Tear the socket and the timer down with the panel, so leaving the view
	// does not leave a session open on a worker.
	$effect(() => () => {
		realtimeSession.destroy();
		if (structureTimer !== null) clearTimeout(structureTimer);
		if (stepsTimer !== null) clearTimeout(stepsTimer);
	});

	function canvasPoint(event: PointerEvent): { x: number; y: number } {
		const canvas = event.currentTarget as HTMLCanvasElement;
		const rect = canvas.getBoundingClientRect();
		return {
			x: ((event.clientX - rect.left) / rect.width) * CANVAS_SIZE,
			y: ((event.clientY - rect.top) / rect.height) * CANVAS_SIZE
		};
	}

	function syncHistory(): void {
		blank = drawingDocument?.isBlank ?? true;
		canUndo = drawingDocument?.canUndo ?? false;
		canRedo = drawingDocument?.canRedo ?? false;
	}

	function finishStroke(): void {
		if (strokePointer === null) return;
		drawingDocument?.finishStroke(strokePointer);
		strokePointer = null;
		syncHistory();
	}

	function onPointerDown(event: PointerEvent): void {
		if (
			openingDrawing ||
			!event.isPrimary ||
			event.button !== 0 ||
			strokePointer !== null ||
			!drawingDocument
		)
			return;
		const point = canvasPoint(event);
		if (
			!drawingDocument.beginStroke(
				event.pointerId,
				{ mode: tool, color: selectedColor, size: brushSize },
				point
			)
		)
			return;
		strokePointer = event.pointerId;
		(event.currentTarget as HTMLCanvasElement).setPointerCapture(event.pointerId);
		realtimeSession.markChanged();
		syncHistory();
	}

	function onPointerMove(event: PointerEvent): void {
		// isPrimary and the stroke's own pointer id: without both, a plain hover
		// after a keyboard-driven pen down would draw, and a second finger would
		// append its moves to the first finger's stroke.
		if (openingDrawing || !event.isPrimary || event.pointerId !== strokePointer) return;
		const point = canvasPoint(event);
		if (drawingDocument?.extendStroke(event.pointerId, point)) {
			realtimeSession.markChanged();
		}
	}

	function onPointerUp(event: PointerEvent): void {
		// The stroke's own pointer id only: another pointer's release must not
		// end this stroke.
		if (event.pointerId !== strokePointer) return;
		finishStroke();
	}

	function clearCanvas(): void {
		if (openingDrawing) return;
		finishStroke();
		if (!drawingDocument?.clear()) return;
		syncHistory();
		realtimeSession.markChanged();
	}

	function undoCanvas(): void {
		if (openingDrawing) return;
		finishStroke();
		if (!drawingDocument?.undo()) return;
		syncHistory();
		realtimeSession.markChanged();
	}

	function redoCanvas(): void {
		if (openingDrawing) return;
		finishStroke();
		if (!drawingDocument?.redo()) return;
		syncHistory();
		realtimeSession.markChanged();
	}

	/** The picker's option label: the model's measured frame cost when its
	 * worker reported one, so the vega-rt versus sdxl-turbo trade-off is
	 * visible before a session starts. */
	function modelOptionLabel(model: Model): string {
		return model.realtime_p95_ms != null
			? `${model.name} - ${Math.round(model.realtime_p95_ms)} ${t('app.realtime_canvas.latency_ms')}`
			: model.name;
	}

	function connect(): void {
		if (!canConnect) return;
		const output = outputCanvas?.getContext('2d');
		if (output) {
			output.fillStyle = PAPER;
			output.fillRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);
		}
		realtimeSession.connect({
			modelId,
			prompt,
			params: { structure_strength: structureValue, steps: stepsValue }
		});
	}

	function applyPrompt(): void {
		if (promptDirty) realtimeSession.updateParams({ prompt });
	}

	function disconnect(): void {
		realtimeSession.disconnect();
	}

	function saveDrawing(): void {
		if (!drawingDocument || openingDrawing) return;
		finishStroke();
		try {
			const file = new Blob([JSON.stringify(drawingDocument.serialize())], {
				type: 'application/json'
			});
			const url = URL.createObjectURL(file);
			const link = document.createElement('a');
			link.href = url;
			link.download = 'drawing.potocolom.json';
			document.body.append(link);
			try {
				link.click();
			} finally {
				link.remove();
				setTimeout(() => URL.revokeObjectURL(url), 0);
			}
			drawingNotice = '';
		} catch {
			drawingNotice = 'app.realtime_canvas.save_failed';
		}
	}

	function chooseDrawingFile(): void {
		if (openingDrawing) return;
		drawingFileInput?.click();
	}

	async function openDrawing(event: Event): Promise<void> {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file || !drawingDocument) return;
		const owner = drawingDocument;
		const request = ++fileRequest;
		openingDrawing = true;
		drawingNotice = '';
		try {
			if (file.size > DRAWING_FILE_MAX_BYTES) throw new Error('drawing file is too large');
			const text = await file.text();
			if (request !== fileRequest || drawingDocument !== owner) return;
			owner.restore(JSON.parse(text));
			strokePointer = null;
			syncHistory();
			realtimeSession.markChanged();
		} catch {
			if (request === fileRequest && drawingDocument === owner)
				drawingNotice = 'app.realtime_canvas.file_invalid';
		} finally {
			if (request === fileRequest) openingDrawing = false;
		}
	}
</script>

<div class="no-scrollbar h-full overflow-y-auto">
	<div class="mx-auto flex h-full w-full max-w-6xl flex-col gap-4">
		<div class="flex flex-wrap items-start justify-between gap-3">
			<div>
				<h1 class="text-xl font-semibold">{t('app.realtime_canvas.title')}</h1>
				<p class="text-muted-foreground mt-1 max-w-3xl text-sm leading-relaxed">
					{t('app.realtime_canvas.sub')}
				</p>
			</div>
			<Badge variant={connection === 'active' ? 'default' : 'outline'}>{statusLabel}</Badge>
		</div>

		<div class="grid flex-none gap-4 lg:min-h-[32rem] lg:flex-1 lg:grid-cols-2">
			<Card.Root class="flex min-h-0 flex-col">
				<Card.Header>
					<Card.Title class="text-base">{t('app.realtime_canvas.input_title')}</Card.Title>
					<Card.Description>{t('app.realtime_canvas.input_sub')}</Card.Description>
				</Card.Header>
				<Card.Content class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
					<div class="flex flex-col gap-2">
						<Label for="realtime-tool">{t('app.realtime_canvas.tool')}</Label>
						<select
							id="realtime-tool"
							bind:value={tool}
							class="border-input bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] h-9 w-full rounded-lg border px-3 text-sm outline-none transition-colors disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50"
						>
							<option value="draw">{t('app.realtime_canvas.tool_draw')}</option>
							<option value="erase">{t('app.realtime_canvas.tool_erase')}</option>
						</select>
					</div>
					<Field.Group class="gap-3">
						<Field.Field>
							<div class="flex items-center justify-between gap-2">
								<Field.Label
									id="realtime-brush-size-label"
									for="realtime-brush-size"
									onclick={() => document.getElementById('realtime-brush-size')?.focus()}
								>
									{t('app.realtime_canvas.brush_size')}
								</Field.Label>
								<span class="text-muted-foreground text-xs tabular-nums">{brushSize}</span>
							</div>
							<Slider
								thumbId="realtime-brush-size"
								labelledBy="realtime-brush-size-label"
								type="single"
								min={1}
								max={32}
								step={1}
								value={brushSize}
								valueText={`${brushSize}`}
								onValueChange={(value) => (brushSize = value)}
							/>
						</Field.Field>
						<Field.Field>
							<Field.Title>{t('app.realtime_canvas.color')}</Field.Title>
							<div
								class="flex flex-wrap gap-2"
								role="group"
								aria-label={t('app.realtime_canvas.color')}
							>
								{#each BRUSH_COLORS as color}
									<Button
										variant={selectedColor === color.value ? 'default' : 'outline'}
										size="icon-sm"
										aria-label={t(color.label)}
										aria-pressed={selectedColor === color.value}
										onclick={() => (selectedColor = color.value)}
										><span class="size-4 rounded-full" style:background-color={color.value}
										></span></Button
									>
								{/each}
							</div>
						</Field.Field>
					</Field.Group>
					<canvas
						bind:this={drawCanvas}
						width={CANVAS_SIZE}
						height={CANVAS_SIZE}
						aria-label={t('app.realtime_canvas.draw_surface')}
						class="border-border mx-auto h-auto w-auto max-h-[min(38vh,calc(100vh-34rem))] max-w-full rounded-lg border bg-white object-contain touch-none"
						onpointerdown={onPointerDown}
						onpointermove={onPointerMove}
						onpointerup={onPointerUp}
						onpointercancel={onPointerUp}
						onlostpointercapture={onPointerUp}
					></canvas>
					<div class="flex items-center justify-between gap-2">
						<div class="flex flex-wrap gap-2">
							<Button
								variant="outline"
								size="sm"
								disabled={openingDrawing || !canUndo}
								onclick={undoCanvas}
							>
								{t('app.realtime_canvas.undo')}
							</Button>
							<Button
								variant="outline"
								size="sm"
								disabled={openingDrawing || !canRedo}
								onclick={redoCanvas}
							>
								{t('app.realtime_canvas.redo')}
							</Button>
							<Button
								variant="outline"
								size="sm"
								disabled={openingDrawing || blank}
								onclick={clearCanvas}
							>
								{t('app.realtime_canvas.clear')}
							</Button>
							<Button variant="outline" size="sm" disabled={openingDrawing} onclick={saveDrawing}>
								{t('app.realtime_canvas.save')}
							</Button>
							<Button
								variant="outline"
								size="sm"
								disabled={openingDrawing}
								onclick={chooseDrawingFile}
							>
								{t('app.realtime_canvas.open')}
							</Button>
							<input
								bind:this={drawingFileInput}
								type="file"
								accept=".potocolom.json,application/json"
								hidden
								onchange={openDrawing}
							/>
						</div>
						<span class="text-muted-foreground text-xs tabular-nums">
							{sentFrames} / {renderedFrames}
						</span>
					</div>
					<p class="text-muted-foreground text-xs">{t('app.realtime_canvas.open_hint')}</p>
					{#if drawingNotice}
						<p class="text-destructive text-sm" role="status" aria-live="polite">
							{t(drawingNotice)}
						</p>
					{/if}
				</Card.Content>
			</Card.Root>

			<Card.Root class="flex min-h-0 flex-col">
				<Card.Header>
					<Card.Title class="text-base">{t('app.realtime_canvas.output_title')}</Card.Title>
					<Card.Description>{t('app.realtime_canvas.output_sub')}</Card.Description>
				</Card.Header>
				<Card.Content class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
					<div class="relative">
						<canvas
							bind:this={outputCanvas}
							width={CANVAS_SIZE}
							height={CANVAS_SIZE}
							class="border-border bg-muted/20 mx-auto h-auto w-auto max-h-[min(38vh,calc(100vh-34rem))] max-w-full rounded-lg border object-contain"
						></canvas>
						{#if renderedFrames === 0}
							<p
								class="text-muted-foreground pointer-events-none absolute inset-0 grid place-items-center px-6 text-center text-sm"
							>
								{t('app.realtime_canvas.output_empty')}
							</p>
						{/if}
					</div>
					<div class="flex flex-col gap-2">
						<!-- The label lives in the picker branch: with one model there is no
						     labelable control for its `for` to point at, and a paragraph
						     cannot carry that id. -->
						{#if realtimeModels.length > 1}
							<Label for="realtime-model">{t('app.realtime_canvas.model')}</Label>
							<select
								id="realtime-model"
								bind:value={modelId}
								disabled={busy}
								class="border-input bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] h-9 w-full rounded-lg border px-3 text-sm outline-none transition-colors disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50"
							>
								{#each realtimeModels as model (model.id)}
									<option value={model.id}>{modelOptionLabel(model)}</option>
								{/each}
							</select>
						{:else}
							<p class="text-sm font-medium">{t('app.realtime_canvas.model')}</p>
							<p class="text-muted-foreground text-sm">{realtimeModels[0]?.name}</p>
						{/if}
						{#if selectedModel?.requires_attribution}
							<p class="text-muted-foreground text-xs">{selectedModel.requires_attribution}</p>
						{/if}
					</div>
					{#if selectedModel}
						<div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
							<ParamSliderField
								id="realtime-structure"
								label={t('app.realtime_canvas.structure')}
								bind:norm={structureNorm}
								steps={trackSteps(structureRange)}
								minLabel={formatParamValue(structureRange.min, structureRange)}
								maxLabel={formatParamValue(structureRange.max, structureRange)}
								valueLabel={formatParamValue(structureValue, structureRange)}
							/>
							<ParamSliderField
								id="realtime-steps"
								label={t('app.realtime_canvas.steps')}
								bind:norm={stepsNorm}
								steps={trackSteps(stepsRange)}
								minLabel={formatParamValue(stepsRange.min, stepsRange)}
								maxLabel={formatParamValue(stepsRange.max, stepsRange)}
								valueLabel={formatParamValue(stepsValue, stepsRange)}
							/>
						</div>
					{/if}
					<div class="flex flex-col gap-2">
						<Label for="realtime-prompt">{t('app.gen.prompt')}</Label>
						<div class="flex gap-2">
							<Input
								id="realtime-prompt"
								bind:value={prompt}
								class="min-w-0"
								placeholder={t('app.realtime_canvas.prompt_placeholder')}
							/>
							<Button variant="outline" size="sm" disabled={!promptDirty} onclick={applyPrompt}>
								{t('app.realtime_canvas.update')}
							</Button>
						</div>
						<p class="text-muted-foreground text-xs">
							{busy
								? t('app.realtime_canvas.prompt_locked')
								: t('app.realtime_canvas.prompt_required')}
						</p>
					</div>
					{#if modelId === ''}
						<p class="text-muted-foreground text-sm">{t('app.realtime_canvas.no_model')}</p>
					{/if}
					{#if notice}
						<p class="text-destructive text-sm" role="status" aria-live="polite">{t(notice)}</p>
					{/if}
					{#if busy}
						<Button variant="secondary" onclick={disconnect}>
							{t('app.realtime_canvas.disconnect')}
						</Button>
					{:else}
						<Button disabled={!canConnect} onclick={connect}>
							{t('app.realtime_canvas.connect')}
						</Button>
					{/if}
				</Card.Content>
			</Card.Root>
		</div>
	</div>
</div>
