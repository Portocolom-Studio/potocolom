<script lang="ts">
	// The live realtime drawing surface (issue #3). One 512 by 512 bitmap, CSS
	// scaled for display, sent as complete WebP frames over the realtime
	// protocol. The session lifecycle lives in $lib/realtime-canvas; this panel
	// keeps the bitmap DOM and drawing controls.
	import { t } from '$lib/i18n.svelte';
	import { loginSearchFor } from '$lib/auth-flow';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { Slider } from '$lib/components/ui/slider';
	import {
		DrawingDocument,
		DRAWING_FILE_MAX_BYTES,
		palmRejected,
		type DrawingTool
	} from '$lib/drawing-document';
	import { lassoToMask, maskOutline, maskWithPrompt, type LassoPoint } from '$lib/canvas-selection';
	import ParamSliderField from '$lib/components/param-slider-field.svelte';
	import {
		formatParamValue,
		modelAcceptsEditPrompt,
		modelProperty,
		normToValue,
		stepsSpec,
		trackSteps,
		structureStrengthSpec,
		valueToNorm
	} from '$lib/model-params';
	import { fallbackModelId, modelIsRemoved, studio, type Model } from '$lib/studio.svelte';
	import {
		createRealtimeCanvasSession,
		isTerminalNotice,
		type ConnectionState,
		type RealtimeCanvasMask,
		type RealtimeCanvasNotice
	} from '$lib/realtime-canvas';
	import { resolve } from '$app/paths';

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
	// The transparent overlays the selection outline is drawn on. Never the
	// canvases themselves: the drawing document owns the first one and the
	// worker's frames paint over the second.
	let drawOutline = $state<HTMLCanvasElement | undefined>();
	let outputOutline = $state<HTMLCanvasElement | undefined>();
	// The panel's own element: Escape is the panel's while focus is inside it.
	let panelRoot = $state<HTMLDivElement | undefined>();
	let drawingDocument = $state<DrawingDocument | null>(null);
	// The pointer type behind the in-progress stroke, kept beside strokePointer:
	// palmRejected reads it to tell a resting palm's touch pointer from a real
	// drawing gesture while a pen owns the stroke.
	let strokePointerType: string | null = null;
	// A pen with no pressure sensor reports a constant 0.5, so a stroke whose
	// first sample is exactly 0.5 is taken as unsensed and paints at full size;
	// a sensing pen lands lighter than that.
	let strokeSensesPressure = false;
	let drawingFileInput = $state<HTMLInputElement | undefined>();

	/** A message is held as its key, not its text, so switching language
	 * retranslates it instead of leaving the previous locale on screen. */
	type NoticeKey = Parameters<typeof t>[0];
	const NOTICE_KEYS: Record<Exclude<RealtimeCanvasNotice, ''>, NoticeKey> = {
		encode_failed: 'app.realtime_canvas.encode_failed',
		decode_failed: 'app.realtime_canvas.decode_failed',
		frame_too_large: 'app.realtime_canvas.frame_too_large',
		socket_error: 'app.realtime_canvas.socket_error',
		refused_protocol: 'app.realtime_canvas.refused_protocol',
		refused_version: 'app.realtime_canvas.refused_version',
		refused_capacity: 'app.realtime_canvas.refused_capacity',
		refused_model: 'app.realtime_canvas.refused_model',
		refused_forbidden: 'app.realtime_canvas.refused_forbidden',
		session_revoked: 'app.realtime_canvas.session_revoked'
	};

	let prompt = $state('');
	let connection = $state<ConnectionState>('idle');
	// The admission queue position the API last reported. Only meaningful while
	// the connection is queued; onState clears it the moment that ends.
	let queuePosition = $state<number | null>(null);
	// The notice as the session reported it, kept beside its translated key
	// because whether it is terminal decides canConnect.
	let rawNotice = $state<RealtimeCanvasNotice>('');
	const notice = $derived(rawNotice === '' ? '' : NOTICE_KEYS[rawNotice]);
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

	let tool = $state<DrawingTool>('draw');
	let openingDrawing = $state(false);
	let fileRequest = 0;
	// The lasso: `selecting` arms the next drag to record a selection instead
	// of a stroke, `selection` is the mask last accepted (and last sent to the
	// session), and `lassoPoints` holds the drag in flight so it can be
	// previewed as it is drawn.
	let selecting = $state(false);
	let selection = $state<RealtimeCanvasMask | null>(null);
	let lassoPoints = $state<LassoPoint[]>([]);
	let lassoPointer: number | null = null;
	// The edit prompt field's own text: what the user last typed. What was
	// actually applied lives on `selection` as `mask.prompt`, so a new lasso
	// can carry an applied prompt over without inventing one.
	let editPrompt = $state('');

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
	const busy = $derived(connection === 'connecting' || connection === 'queued' || connected);
	const canConnect = $derived(
		!busy && !isTerminalNotice(rawNotice) && modelId !== '' && prompt.trim() !== ''
	);
	// The prompt differs from the last one the API confirmed; whitespace around
	// it does not count, because openMessage and updateParamsMessage both trim.
	const promptDirty = $derived(connected && prompt.trim() !== appliedPrompt);
	// The selection controls belong to a model whose manifest declares the mask
	// param, read the way the sliders read theirs: only sdxl-turbo and vega-rt
	// declare it, and for any other realtime model a mask would be a param no
	// worker understands. The outline mounts only while one can be outlined,
	// which is also what keeps the draw canvas first and the output canvas
	// second in the document's canvas order.
	const supportsSelection = $derived(modelProperty(selectedModel, 'mask') !== undefined);
	// The edit prompt field mounts one step further in: the same manifest read,
	// but for a mask that is an object carrying a `prompt` of its own, so the
	// panel never offers a field the worker would drop.
	const supportsEditPrompt = $derived(modelAcceptsEditPrompt(selectedModel));
	// Whether the field differs from the prompt the selection last sent.
	const editPromptDirty = $derived(editPrompt.trim() !== (selection?.prompt ?? ''));
	const showOutline = $derived(selection !== null || lassoPoints.length > 0);

	const realtimeSession = createRealtimeCanvasSession({
		getDrawCanvas: () => drawCanvas,
		getOutputCanvas: () => outputCanvas,
		isCanvasBlank: () => drawingDocument?.isBlank ?? true,
		onState: (state) => {
			connection = state;
			if (state !== 'queued') queuePosition = null;
		},
		onQueuePosition: (position) => {
			queuePosition = position;
		},
		onNotice: (key: RealtimeCanvasNotice) => {
			rawNotice = key;
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
	const statusLabel = $derived(
		connection === 'queued' && queuePosition !== null
			? t('app.realtime_canvas.status_queued_position').replace('{position}', String(queuePosition))
			: t(STATUS_KEYS[connection])
	);

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

	$effect(() => {
		// A selection lives in the session's params, so it cannot outlive the
		// session: once nothing is connecting, queued or live, drop it, which
		// also covers a model change (the picker only unlocks then). The edit
		// prompt goes with it: with no selection there is nothing to apply it
		// to, and leftover text would sit in the field while the next lasso
		// applied none of it.
		if (!busy) {
			selection = null;
			editPrompt = '';
			selecting = false;
			dropLasso();
		}
	});

	$effect(() => {
		// The outline itself: the mask last accepted, or the lasso being drawn
		// right now. It is painted on the overlays, never on the canvases, so
		// the drawing document and the worker's frames stay untouched. It sits
		// on a picture, not on the page, so no theme colour is safe: a white
		// line under a dark dash reads on any image, light or dark.
		const points =
			lassoPoints.length > 0
				? lassoPoints
				: selection
					? maskOutline(selection, CANVAS_SIZE, CANVAS_SIZE)
					: [];
		for (const overlay of [drawOutline, outputOutline]) {
			if (!overlay) continue;
			const context = overlay.getContext('2d');
			if (!context) continue;
			context.clearRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);
			if (points.length < 2) continue;
			context.save();
			context.lineWidth = 2;
			context.lineJoin = 'round';
			context.beginPath();
			context.moveTo(points[0].x, points[0].y);
			for (const point of points) context.lineTo(point.x, point.y);
			// A held selection is a closed polygon; the drag in flight is not
			// closed until the release accepts it.
			if (lassoPoints.length === 0) context.closePath();
			context.strokeStyle = '#ffffff';
			context.stroke();
			context.setLineDash([6, 4]);
			context.strokeStyle = '#111827';
			context.stroke();
			context.restore();
		}
	});

	// Tear the socket and the timer down with the panel, so leaving the view
	// does not leave a session open on a worker.
	$effect(() => () => {
		realtimeSession.destroy();
		if (structureTimer !== null) clearTimeout(structureTimer);
		if (stepsTimer !== null) clearTimeout(stepsTimer);
	});

	function canvasPoint(event: PointerEvent): { x: number; y: number; pressure?: number } {
		const canvas = event.currentTarget as HTMLCanvasElement;
		const rect = canvas.getBoundingClientRect();
		const point = {
			x: ((event.clientX - rect.left) / rect.width) * CANVAS_SIZE,
			y: ((event.clientY - rect.top) / rect.height) * CANVAS_SIZE
		};
		// Mouse reports 0.5 while a button is held and touch reports its own
		// contact pressure, so only a sensing pen's nonzero reading is real.
		if (strokeSensesPressure && event.pointerType === 'pen' && event.pressure > 0) {
			// Three decimals is finer than a pen resolves and keeps a long stroke
			// well inside the drawing file's byte limit.
			return { ...point, pressure: Math.round(event.pressure * 1000) / 1000 };
		}
		return point;
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
		strokePointerType = null;
		syncHistory();
	}

	function onPointerDown(event: PointerEvent): void {
		if (openingDrawing || !event.isPrimary || event.button !== 0) return;
		if (palmRejected(strokePointerType, event.pointerType)) return;
		if (selecting) {
			if (lassoPointer !== null || strokePointer !== null) return;
			lassoPointer = event.pointerId;
			lassoPoints = [canvasPoint(event)];
			(event.currentTarget as HTMLCanvasElement).setPointerCapture(event.pointerId);
			return;
		}
		if (strokePointer !== null || !drawingDocument) return;
		strokeSensesPressure = event.pointerType === 'pen' && event.pressure !== 0.5;
		const point = canvasPoint(event);
		if (
			!drawingDocument.beginStroke(
				event.pointerId,
				{
					tool,
					color: selectedColor,
					size: brushSize
				},
				point
			)
		)
			return;
		strokePointer = event.pointerId;
		strokePointerType = event.pointerType;
		(event.currentTarget as HTMLCanvasElement).setPointerCapture(event.pointerId);
		realtimeSession.markChanged();
		syncHistory();
	}

	// The output canvas takes a lasso and nothing else: drawing belongs to the
	// sketch, and a drag on the picture must not add strokes to it.
	function onOutputPointerDown(event: PointerEvent): void {
		if (selecting) onPointerDown(event);
	}

	function onPointerMove(event: PointerEvent): void {
		// isPrimary and the stroke's own pointer id: without both, a plain hover
		// after a keyboard-driven pen down would draw, and a second finger would
		// append its moves to the first finger's stroke. The lasso answers to
		// its own pointer id for the same reason, and adds nothing to the
		// drawing document: a selection changes what renders, not what is drawn.
		if (openingDrawing || !event.isPrimary) return;
		if (palmRejected(strokePointerType, event.pointerType)) return;
		if (event.pointerId === lassoPointer) {
			lassoPoints = [...lassoPoints, canvasPoint(event)];
			return;
		}
		if (event.pointerId !== strokePointer) return;
		const point = canvasPoint(event);
		if (drawingDocument?.extendStroke(event.pointerId, point)) {
			realtimeSession.markChanged();
		}
	}

	// A cancel or a lost capture drops a lasso rather than sending it: a stroke
	// cut short stays local, but a mask changes what the worker renders.
	function onPointerCancel(event: PointerEvent): void {
		if (event.pointerId === lassoPointer) {
			dropLasso();
			return;
		}
		onPointerUp(event);
	}

	function dropLasso(): void {
		lassoPointer = null;
		lassoPoints = [];
	}

	function onPointerUp(event: PointerEvent): void {
		// The lasso's own pointer id first: releasing it accepts or drops the
		// selection, and must not reach the stroke below.
		if (event.pointerId === lassoPointer) {
			finishLasso();
			return;
		}
		// The stroke's own pointer id only: another pointer's release must not
		// end this stroke.
		if (event.pointerId !== strokePointer) return;
		finishStroke();
	}

	/** Close the drag in flight. A click or a scribble yields no mask: the
	 * toggle stays armed so the next drag can select, and the selection the
	 * panel already had is left alone. */
	function finishLasso(): void {
		const points = lassoPoints;
		dropLasso();
		const mask = lassoToMask(points, CANVAS_SIZE, CANVAS_SIZE);
		if (!mask || !connected) return;
		// The field is what the edit prompt is: a new lasso takes its current
		// text, so what the person sees in the field is what the worker gets.
		selection = maskWithPrompt(mask, supportsEditPrompt ? editPrompt : undefined);
		selecting = false;
		realtimeSession.updateParams({ mask: selection });
	}

	function toggleSelecting(): void {
		selecting = !selecting;
		if (!selecting) dropLasso();
	}

	/** Drop the selection here and in the session's params, so the next frames
	 * are generated whole again. The edit prompt goes with it: it described
	 * that area only. */
	function clearSelection(): void {
		if (!connected) return;
		selection = null;
		editPrompt = '';
		realtimeSession.updateParams({ mask: null });
	}

	/** Send the field's text as the selection's own prompt, trimmed. An empty
	 * field sends the mask without `prompt`, which puts the selection back on
	 * the session prompt for the frames inside it. */
	function applyEditPrompt(): void {
		if (!connected || selection === null) return;
		selection = maskWithPrompt(selection, editPrompt);
		realtimeSession.updateParams({ mask: selection });
	}

	/** Enter applies the prompt from within the field. Escape is handled
	 * nowhere here on purpose: the window handler's text-field guard already
	 * leaves it to the field, so it never clears the selection. */
	function onEditPromptKeydown(event: KeyboardEvent): void {
		// Enter that commits an IME composition is not a request to apply.
		if (event.key !== 'Enter' || event.isComposing || !editPromptDirty) return;
		applyEditPrompt();
	}

	/** Paint paper over the sketch inside the selection, as one undoable edit.
	 * The selection itself stays: the usual next step is drawing inside it. */
	function eraseSelection(): void {
		if (openingDrawing || selection === null) return;
		finishStroke();
		if (!drawingDocument?.eraseRegion(maskOutline(selection, CANVAS_SIZE, CANVAS_SIZE))) return;
		syncHistory();
		realtimeSession.markChanged();
	}

	/** A modal surface owns Escape while it is open, and the panel's own
	 * controls are not modal surfaces. */
	function modalIsOpen(): boolean {
		return document.querySelector('[role="dialog"][aria-modal="true"], dialog[open]') !== null;
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		// Escape is the panel's only when focus is inside it: anywhere else on
		// the page the key belongs to whatever holds focus.
		if (event.key !== 'Escape' || selection === null || modalIsOpen()) return;
		if (!panelRoot?.contains(document.activeElement)) return;
		// In a text field Escape belongs to the field (abandoning an edit), not
		// to the selection.
		const target = event.target as HTMLElement | null;
		if (event.defaultPrevented || target?.closest('input, textarea, select')) return;
		clearSelection();
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
		// A new session opens with the params above, which carry no mask: an
		// outline left over from the previous one would describe a selection
		// the worker never received, and an edit prompt left over would
		// describe that same selection. Model changes land here too, because the
		// picker only allows them while no session is running.
		selection = null;
		editPrompt = '';
		lassoPoints = [];
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
			strokePointerType = null;
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

<!-- Escape clears the selection while focus is inside the panel, so the window
     handler asks the panel's own root before acting. -->
<svelte:window onkeydown={onWindowKeydown} />

<div class="no-scrollbar h-full overflow-y-auto" bind:this={panelRoot}>
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
			<!-- The drawing toolbar spans both columns so its five actions and the
			     frame counter stay on one row once the grid is two columns wide;
			     below that it stacks into two lines instead of orphaning a button. -->
			<div class="flex flex-col gap-2 lg:col-span-2 lg:flex-row lg:items-center lg:justify-between">
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
						aria-describedby="realtime-open-hint"
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
			<p id="realtime-open-hint" class="text-muted-foreground text-xs lg:col-span-2">
				{t('app.realtime_canvas.open_hint')}
			</p>
			<Card.Root class="flex min-h-0 flex-col">
				<Card.Header>
					<Card.Title class="text-base">{t('app.realtime_canvas.input_title')}</Card.Title>
					<Card.Description>{t('app.realtime_canvas.input_sub')}</Card.Description>
				</Card.Header>
				<Card.Content class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
					<div class="flex flex-col gap-2">
						<Label for="realtime-tool">{t('app.realtime_canvas.tool')}</Label>
						<div class="flex flex-wrap items-center gap-2">
							<select
								id="realtime-tool"
								bind:value={tool}
								class="border-input bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] h-9 min-w-0 flex-1 rounded-lg border px-3 font-sans text-sm outline-none transition-colors disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50"
							>
								<option value="draw">{t('app.realtime_canvas.tool_draw')}</option>
								<option value="erase">{t('app.realtime_canvas.tool_erase')}</option>
								<option value="line">{t('app.realtime_canvas.tool_line')}</option>
								<option value="rectangle">{t('app.realtime_canvas.tool_rectangle')}</option>
								<option value="ellipse">{t('app.realtime_canvas.tool_ellipse')}</option>
							</select>
							{#if supportsSelection}
								<Button
									variant={selecting ? 'default' : 'outline'}
									aria-pressed={selecting}
									disabled={!connected}
									onclick={toggleSelecting}
								>
									{t('app.realtime_canvas.select_area')}
								</Button>
								{#if selection}
									<Button variant="outline" disabled={!connected} onclick={clearSelection}>
										{t('app.realtime_canvas.clear_selection')}
									</Button>
									<Button variant="outline" disabled={openingDrawing} onclick={eraseSelection}>
										{t('app.realtime_canvas.erase_selection')}
									</Button>
								{/if}
							{/if}
						</div>
					</div>
					{#if selection && supportsEditPrompt}
						<div class="flex flex-col gap-2">
							<Label for="realtime-edit-prompt">{t('app.realtime_canvas.edit_prompt')}</Label>
							<div class="flex gap-2">
								<Input
									id="realtime-edit-prompt"
									bind:value={editPrompt}
									class="min-w-0"
									onkeydown={onEditPromptKeydown}
								/>
								<Button
									variant="outline"
									size="sm"
									disabled={!connected || !editPromptDirty}
									onclick={applyEditPrompt}
								>
									{t('app.realtime_canvas.edit_prompt_apply')}
								</Button>
							</div>
							<p class="text-muted-foreground text-xs">
								{t('app.realtime_canvas.edit_prompt_hint')}
							</p>
						</div>
					{/if}
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
					<div class="relative mx-auto w-fit max-w-full">
						<canvas
							bind:this={drawCanvas}
							width={CANVAS_SIZE}
							height={CANVAS_SIZE}
							aria-label={t('app.realtime_canvas.draw_surface')}
							class="border-border block h-auto w-auto max-h-[min(38vh,calc(100vh-34rem))] max-w-full rounded-lg border bg-white object-contain touch-none"
							onpointerdown={onPointerDown}
							onpointermove={onPointerMove}
							onpointerup={onPointerUp}
							onpointercancel={onPointerCancel}
							onlostpointercapture={onPointerCancel}
						></canvas>
						{#if showOutline}
							<canvas
								bind:this={drawOutline}
								width={CANVAS_SIZE}
								height={CANVAS_SIZE}
								aria-hidden="true"
								class="pointer-events-none absolute inset-0 h-full w-full rounded-lg"
							></canvas>
						{/if}
					</div>
					{#if supportsSelection && selection}
						<p class="text-muted-foreground text-xs">
							{t('app.realtime_canvas.selection_hint')}
						</p>
					{/if}
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
						<div class="relative mx-auto w-fit max-w-full">
							<canvas
								bind:this={outputCanvas}
								width={CANVAS_SIZE}
								height={CANVAS_SIZE}
								class="border-border bg-muted/20 block h-auto w-auto max-h-[min(38vh,calc(100vh-34rem))] max-w-full rounded-lg border object-contain"
								class:touch-none={selecting}
								onpointerdown={onOutputPointerDown}
								onpointermove={onPointerMove}
								onpointerup={onPointerUp}
								onpointercancel={onPointerCancel}
								onlostpointercapture={onPointerCancel}
							></canvas>
							{#if showOutline}
								<canvas
									bind:this={outputOutline}
									width={CANVAS_SIZE}
									height={CANVAS_SIZE}
									aria-hidden="true"
									class="pointer-events-none absolute inset-0 h-full w-full rounded-lg"
								></canvas>
							{/if}
						</div>
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
								class="border-input bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] h-9 w-full rounded-lg border px-3 font-sans text-sm outline-none transition-colors disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50"
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
						<p class="text-destructive text-sm" role="status" aria-live="polite">
							{t(notice)}
							{#if notice === 'app.realtime_canvas.session_revoked'}
								{' '}
								<a
									class="text-foreground underline underline-offset-4"
									href={`${resolve('/login')}${loginSearchFor('?view=realtime_canvas')}`}
									>{t('app.realtime_canvas.sign_in_again')}</a
								>
							{/if}
						</p>
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
