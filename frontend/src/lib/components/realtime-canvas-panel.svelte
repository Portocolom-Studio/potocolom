<script lang="ts">
	// The live realtime drawing surface (issue #3). One 512 by 512 bitmap, CSS
	// scaled for display, sent as complete WebP frames over the realtime
	// protocol. The session lifecycle lives in $lib/realtime-canvas; this panel
	// keeps the bitmap DOM and drawing controls.
	import type { Component } from 'svelte';
	import { MediaQuery } from 'svelte/reactivity';
	import BrushIcon from '@lucide/svelte/icons/brush';
	import CameraIcon from '@lucide/svelte/icons/camera';
	import ChevronsUpDownIcon from '@lucide/svelte/icons/chevrons-up-down';
	import CircleIcon from '@lucide/svelte/icons/circle';
	import Columns2Icon from '@lucide/svelte/icons/columns-2';
	import DownloadIcon from '@lucide/svelte/icons/download';
	import EraserIcon from '@lucide/svelte/icons/eraser';
	import FolderOpenIcon from '@lucide/svelte/icons/folder-open';
	import LassoIcon from '@lucide/svelte/icons/lasso';
	import Maximize2Icon from '@lucide/svelte/icons/maximize-2';
	import MinusIcon from '@lucide/svelte/icons/minus';
	import PowerIcon from '@lucide/svelte/icons/power';
	import Redo2Icon from '@lucide/svelte/icons/redo-2';
	import SaveIcon from '@lucide/svelte/icons/save';
	import SlidersHorizontalIcon from '@lucide/svelte/icons/sliders-horizontal';
	import SquareIcon from '@lucide/svelte/icons/square';
	import Trash2Icon from '@lucide/svelte/icons/trash-2';
	import Undo2Icon from '@lucide/svelte/icons/undo-2';
	import ZapIcon from '@lucide/svelte/icons/zap';
	import { t } from '$lib/i18n.svelte';
	import { loginSearchFor } from '$lib/auth-flow';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { Slider } from '$lib/components/ui/slider';
	import { Kbd } from '$lib/components/ui/kbd';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import { toast } from 'svelte-sonner';
	import { offerUndo } from '$lib/studio-toasts';
	import * as Tooltip from '$lib/components/ui/tooltip';
	import {
		ariaKeyShortcuts,
		canvasShortcut,
		isTypingTarget,
		SELECT_KEY,
		stepBrushSize,
		TOOL_KEYS,
		type ShortcutKey
	} from '$lib/shortcuts';
	import { platform, shortcutLabel } from '$lib/platform.svelte';
	import { withViewTransition } from '$lib/view-transition';
	import { estimatePromptTokens } from '$lib/prompt-tokens';
	import { readStored, writeStored } from '$lib/safe-storage';
	import { useStudioHeader, useViewCommands } from '$lib/studio-chrome.svelte';
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
		type FrameCounters,
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
	// Every stage of the frame path behind the two numbers above, shown as the
	// readout's title rather than on it: the row has room for sent and shown.
	let stageCounters = $state<FrameCounters>({
		attempted: 0,
		replaced: 0,
		encoded: 0,
		sent: 0,
		generated: 0,
		presented: 0
	});
	const frameCounterTitle = $derived(
		t('app.realtime_canvas.frame_counters')
			.replace('{attempted}', () => String(stageCounters.attempted))
			.replace('{replaced}', () => String(stageCounters.replaced))
			.replace('{encoded}', () => String(stageCounters.encoded))
			.replace('{sent}', () => String(stageCounters.sent))
			.replace('{generated}', () => String(stageCounters.generated))
			.replace('{presented}', () => String(stageCounters.presented))
	);
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
		onCounters: (counters) => {
			sentFrames = counters.sent;
			renderedFrames = counters.presented;
			stageCounters = counters;
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

	// Bumped on every change to the drawing's history, so the Undo offered
	// after Clear canvas can tell whether the clear is still the last edit.
	let historyVersion = 0;
	let clearOffer: { toast: string | number; version: number } | null = null;

	function syncHistory(): void {
		blank = drawingDocument?.isBlank ?? true;
		canUndo = drawingDocument?.canUndo ?? false;
		canRedo = drawingDocument?.canRedo ?? false;
		historyVersion += 1;
		// Anything drawn, undone or opened after a clear withdraws its Undo
		// offer: undoing then would take back that edit instead.
		if (clearOffer !== null && clearOffer.version !== historyVersion) withdrawClearOffer();
	}

	function withdrawClearOffer(): void {
		if (clearOffer === null) return;
		toast.dismiss(clearOffer.toast);
		clearOffer = null;
	}
	$effect(() => withdrawClearOffer);

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

	function selectWholeCanvas(): void {
		lassoPoints = [
			{ x: 0, y: 0 },
			{ x: CANVAS_SIZE, y: 0 },
			{ x: CANVAS_SIZE, y: CANVAS_SIZE },
			{ x: 0, y: CANVAS_SIZE }
		];
		finishLasso();
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

	/** The tool, brush and history keys. They belong to the canvas while this
	 * view is open, except in a text field (typing a B is not picking the
	 * brush) or under a dialog. */
	function onCanvasShortcut(event: KeyboardEvent): boolean {
		if (event.defaultPrevented || event.isComposing || isTypingTarget(event.target)) return false;
		if (modalIsOpen()) return false;
		const shortcut = canvasShortcut(event, platform.apple);
		if (shortcut === null) return false;
		if (shortcut.kind === 'undo') {
			if (openingDrawing || !canUndo) return false;
			undoCanvas();
		} else if (shortcut.kind === 'redo') {
			if (openingDrawing || !canRedo) return false;
			redoCanvas();
		} else if (shortcut.kind === 'brush') {
			brushSize = stepBrushSize(brushSize, shortcut.delta);
		} else if (event.repeat) {
			// A held letter would toggle the lasso on and off.
			return false;
		} else if (shortcut.kind === 'select') {
			if (!supportsSelection || !connected) return false;
			toggleSelecting();
		} else {
			tool = shortcut.tool;
		}
		event.preventDefault();
		return true;
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if (onCanvasShortcut(event)) return;
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

	function onBeforeUnload(event: BeforeUnloadEvent): void {
		if (!blank) event.preventDefault();
	}

	function clearCanvas(): void {
		if (openingDrawing) return;
		finishStroke();
		if (!drawingDocument?.clear()) return;
		withdrawClearOffer();
		syncHistory();
		realtimeSession.markChanged();
		// The clear is an operation in the drawing's own history, so Undo here
		// is the same undo as the button, and Redo clears again.
		const version = historyVersion;
		const id = offerUndo(t('app.toast.canvas_cleared'), () => {
			if (clearOffer?.version !== version) return;
			clearOffer = null;
			undoCanvas();
		});
		clearOffer = { toast: id, version };
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
	// Workbench shows the sketch and the output side by side with an
	// inspector; Focus lays the sketch over a large output at a chosen
	// opacity. Both arrange the same canvases, so switching never touches the
	// drawing or the session. Focus needs a wide screen; a narrow one always
	// gets the stacked workbench.
	type ViewMode = 'workbench' | 'focus';
	const VIEW_STORAGE_KEY = 'realtime-canvas-view';
	let viewMode = $state<ViewMode>(readStored(VIEW_STORAGE_KEY) === 'focus' ? 'focus' : 'workbench');
	const wide = new MediaQuery('min-width: 1024px', false);
	const focus = $derived(viewMode === 'focus' && wide.current);
	let sketchOpacity = $state(32);
	const VIEW_MODES: { mode: ViewMode; icon: Component; label: NoticeKey }[] = [
		{ mode: 'workbench', icon: Columns2Icon, label: 'app.realtime_canvas.view_workbench' },
		{ mode: 'focus', icon: Maximize2Icon, label: 'app.realtime_canvas.view_focus' }
	];
	let guidanceOpen = $state(false);

	function setViewMode(mode: ViewMode): void {
		if (mode === viewMode) return;
		withViewTransition(() => {
			viewMode = mode;
		});
		writeStored(VIEW_STORAGE_KEY, mode);
	}

	const UNDO_KEYS: ShortcutKey[] = ['mod', 'z'];
	const REDO_KEYS: ShortcutKey[] = ['shift', 'mod', 'z'];
	const TOOLS: { value: DrawingTool; label: NoticeKey; icon: Component }[] = [
		{ value: 'draw', label: 'app.realtime_canvas.tool_draw', icon: BrushIcon },
		{ value: 'erase', label: 'app.realtime_canvas.tool_erase', icon: EraserIcon },
		{ value: 'line', label: 'app.realtime_canvas.tool_line', icon: MinusIcon },
		{ value: 'rectangle', label: 'app.realtime_canvas.tool_rectangle', icon: SquareIcon },
		{ value: 'ellipse', label: 'app.realtime_canvas.tool_ellipse', icon: CircleIcon }
	];

	// Frames shown per second, from the presented counter over the last few
	// seconds. Only while a session is live; nothing is invented when idle.
	let fps = $state<number | null>(null);
	let fpsSamples: { at: number; presented: number }[] = [];
	let sessionStartedAt = $state<number | null>(null);
	let sessionNow = $state(0);

	function sampleFps(): void {
		const at = performance.now();
		fpsSamples = [...fpsSamples, { at, presented: renderedFrames }].filter(
			(sample) => at - sample.at <= 3000
		);
		const first = fpsSamples[0];
		const last = fpsSamples[fpsSamples.length - 1];
		fps =
			first && last && last.at - first.at >= 900
				? ((last.presented - first.presented) * 1000) / (last.at - first.at)
				: null;
	}

	$effect(() => {
		if (!connected) {
			fps = null;
			fpsSamples = [];
			if (!busy) sessionStartedAt = null;
			return;
		}
		if (sessionStartedAt === null) sessionStartedAt = Date.now();
		sessionNow = Date.now();
		const timer = setInterval(() => {
			sampleFps();
			sessionNow = Date.now();
		}, 1000);
		return () => clearInterval(timer);
	});

	const sessionElapsed = $derived.by(() => {
		if (sessionStartedAt === null) return null;
		const seconds = Math.max(0, Math.floor((sessionNow - sessionStartedAt) / 1000));
		return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
	});

	$effect(() => {
		studio.realtimeLive = connected;
	});
	$effect(() => () => {
		studio.realtimeLive = false;
	});

	const promptTokens = $derived(estimatePromptTokens(prompt));
	const promptLimit = $derived(selectedModel?.prompt_token_limit ?? 0);
	const tokenLabel = $derived(
		promptLimit > 0
			? t('app.realtime_canvas.tokens_of')
					.replace('{tokens}', String(promptTokens))
					.replace('{limit}', String(promptLimit))
			: t('app.realtime_canvas.tokens').replace('{tokens}', String(promptTokens))
	);

	/** Cmd or Ctrl and Enter: update a live session, or start one. */
	function onPromptKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Enter' || event.isComposing) return;
		const submit =
			event.target instanceof HTMLTextAreaElement ? event.metaKey || event.ctrlKey : true;
		if (!submit) return;
		event.preventDefault();
		if (connected) applyPrompt();
		else connect();
	}

	// Captures: the output as it is now, kept in this tab only (object URLs,
	// never uploaded) so a frame worth keeping can be downloaded later.
	const MAX_CAPTURES = 8;
	let captures = $state<{ id: number; url: string }[]>([]);
	let captureSequence = 0;

	function downloadUrl(url: string, name: string): void {
		const link = document.createElement('a');
		link.href = url;
		link.download = name;
		document.body.append(link);
		try {
			link.click();
		} finally {
			link.remove();
		}
	}

	function captureOutput(): void {
		if (!outputCanvas || renderedFrames === 0) return;
		outputCanvas.toBlob((blob) => {
			if (!blob) return;
			captureSequence += 1;
			const next = [{ id: captureSequence, url: URL.createObjectURL(blob) }, ...captures];
			for (const dropped of next.slice(MAX_CAPTURES)) URL.revokeObjectURL(dropped.url);
			captures = next.slice(0, MAX_CAPTURES);
		}, 'image/png');
	}

	function downloadOutput(): void {
		if (!outputCanvas || renderedFrames === 0) return;
		outputCanvas.toBlob((blob) => {
			if (!blob) return;
			const url = URL.createObjectURL(blob);
			downloadUrl(url, 'potocolom-frame.png');
			setTimeout(() => URL.revokeObjectURL(url), 0);
		}, 'image/png');
	}

	$effect(() => () => {
		for (const capture of captures) URL.revokeObjectURL(capture.url);
	});

	const statusTone = $derived(
		connection === 'active'
			? 'bg-status-success'
			: connection === 'failed' || connection === 'interrupted'
				? 'bg-status-error'
				: connection === 'queued'
					? 'bg-status-warning'
					: connection === 'idle'
						? 'bg-dim'
						: 'bg-status-running'
	);

	useStudioHeader(() => ({ center: viewToggle, actions: sessionActions }));
	// Focus needs a wide screen, so the palette offers the switch only there,
	// as the header toggle does.
	useViewCommands(() =>
		wide.current
			? [
					viewMode === 'focus'
						? {
								id: 'canvas-workbench',
								label: t('app.realtime_canvas.command_workbench'),
								hint: t('app.realtime_canvas.view_mode'),
								icon: Columns2Icon,
								run: () => setViewMode('workbench')
							}
						: {
								id: 'canvas-focus',
								label: t('app.realtime_canvas.command_focus'),
								hint: t('app.realtime_canvas.view_mode'),
								icon: Maximize2Icon,
								run: () => setViewMode('focus')
							}
				]
			: []
	);
</script>

<!-- Escape clears the selection while focus is inside the panel, so the window
     handler asks the panel's own root before acting. -->
<svelte:window onkeydown={onWindowKeydown} onbeforeunload={onBeforeUnload} />

{#snippet viewToggle()}
	<div
		class="bg-field border-line-strong my-auto ms-3 hidden gap-0.5 rounded-[7px] border p-0.5 lg:flex"
		role="group"
		aria-label={t('app.realtime_canvas.view_mode')}
	>
		{#each VIEW_MODES as { mode, icon: Icon, label } (mode)}
			<button
				type="button"
				class={'flex h-[26px] items-center gap-1.5 rounded-[5px] px-2.5 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
					(viewMode === mode
						? 'bg-hover text-foreground ring-line-strong shadow-[0_1px_2px_rgba(0,0,0,0.4)] ring-1 ring-inset'
						: 'text-muted-foreground hover:text-foreground')}
				aria-pressed={viewMode === mode}
				onclick={() => setViewMode(mode)}
			>
				<Icon class="size-3.5" aria-hidden="true" />
				{t(label)}
			</button>
		{/each}
	</div>
{/snippet}

{#snippet sessionActions()}
	<div class="flex items-center gap-3.5 text-[12.5px]">
		<span role="status" class="flex items-center gap-2 font-medium">
			<span class={'size-[7px] shrink-0 rounded-full ' + statusTone} aria-hidden="true"></span>
			{statusLabel}
		</span>
		{#if connected && selectedModel?.realtime_p95_ms != null}
			<span class="text-muted-foreground hidden tabular-nums md:inline">
				<b class="text-foreground font-medium">{Math.round(selectedModel.realtime_p95_ms)}</b>
				{t('app.realtime_canvas.ms_p95')}
			</span>
		{/if}
		{#if connected && fps !== null}
			<span class="text-muted-foreground hidden tabular-nums md:inline">
				<b class="text-foreground font-medium">{fps.toFixed(1)}</b> fps
			</span>
		{/if}
	</div>
	{#if busy}
		<Button variant="outline" size="sm" onclick={disconnect}>
			<PowerIcon />
			{t('app.realtime_canvas.disconnect')}
		</Button>
	{:else}
		<Button variant="brand" size="sm" disabled={!canConnect} onclick={connect}>
			<PowerIcon />
			{t('app.realtime_canvas.connect')}
		</Button>
	{/if}
{/snippet}

<!-- A control's name with its key beside it. The tooltip replaces the title
     the buttons used to carry; the accessible name stays on aria-label and the
     key on aria-keyshortcuts. -->
{#snippet hinted(
	label: string,
	keys: ShortcutKey[],
	side: 'top' | 'right' | 'bottom' | 'left',
	trigger: import('svelte').Snippet<[Record<string, unknown>]>
)}
	<Tooltip.Root>
		<Tooltip.Trigger>
			{#snippet child({ props })}
				{@render trigger(props)}
			{/snippet}
		</Tooltip.Trigger>
		<Tooltip.Content {side}>
			{label}
			<Kbd>{shortcutLabel(keys)}</Kbd>
		</Tooltip.Content>
	</Tooltip.Root>
{/snippet}

{#snippet toolRail()}
	<div class="rail" role="group" aria-label={t('app.realtime_canvas.tool')}>
		{#each TOOLS as option (option.value)}
			{#snippet toolButton(props: Record<string, unknown>)}
				<button
					{...props}
					type="button"
					class="rail-button hit-slop"
					data-tool={option.value}
					aria-label={t(option.label)}
					aria-keyshortcuts={ariaKeyShortcuts([TOOL_KEYS[option.value]])}
					aria-pressed={tool === option.value}
					onclick={() => (tool = option.value)}
				>
					<option.icon class="size-[17px]" aria-hidden="true" />
				</button>
			{/snippet}
			{@render hinted(
				t(option.label),
				[TOOL_KEYS[option.value]],
				wide.current ? 'right' : 'bottom',
				toolButton
			)}
		{/each}
		{#if supportsSelection}
			<span class="rail-divider" aria-hidden="true"></span>
			{#snippet selectButton(props: Record<string, unknown>)}
				<button
					{...props}
					type="button"
					class="rail-button hit-slop"
					aria-label={t('app.realtime_canvas.select_area')}
					aria-keyshortcuts={ariaKeyShortcuts([SELECT_KEY])}
					aria-pressed={selecting}
					disabled={!connected}
					onclick={toggleSelecting}
				>
					<LassoIcon class="size-[17px]" aria-hidden="true" />
				</button>
			{/snippet}
			{@render hinted(
				t('app.realtime_canvas.select_area'),
				[SELECT_KEY],
				wide.current ? 'right' : 'bottom',
				selectButton
			)}
		{/if}
	</div>
{/snippet}

{#snippet paint()}
	<div class="paint">
		<div class="swatches" role="group" aria-label={t('app.realtime_canvas.color')}>
			{#each BRUSH_COLORS as color (color.value)}
				<button
					type="button"
					class="swatch hit-slop"
					aria-label={t(color.label)}
					aria-pressed={selectedColor === color.value}
					onclick={() => (selectedColor = color.value)}
					style:background-color={color.value}
				></button>
			{/each}
		</div>
		<div class="size">
			<span id="realtime-brush-size-label" class="sr-only"
				>{t('app.realtime_canvas.brush_size')}</span
			>
			<button
				type="button"
				class="text-muted-foreground text-xs"
				aria-hidden="true"
				tabindex="-1"
				onclick={() => document.getElementById('realtime-brush-size')?.focus()}
				>{t('app.realtime_canvas.size_short')}</button
			>
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
				class="size-slider"
			/>
			<span class="text-foreground w-7 text-xs tabular-nums"
				>{brushSize}<span class="size-unit">px</span></span
			>
		</div>
	</div>
{/snippet}

{#snippet selectionControls()}
	{#if supportsSelection && (selecting || selection)}
		<div class="flex flex-col gap-2.5">
			<div class="flex items-center justify-between gap-2">
				<h2 class="text-xs font-semibold">{t('app.realtime_canvas.selection_edit')}</h2>
				{#if selection}
					<span class="tag tag-info">{t('app.realtime_canvas.area_selected')}</span>
				{/if}
			</div>
			{#if selection && supportsEditPrompt}
				<Label for="realtime-edit-prompt" class="sr-only"
					>{t('app.realtime_canvas.edit_prompt')}</Label
				>
				<div class="flex gap-2">
					<Input
						id="realtime-edit-prompt"
						name="edit_prompt"
						autocomplete="off"
						bind:value={editPrompt}
						placeholder={t('app.realtime_canvas.edit_prompt')}
						class="min-w-0"
						onkeydown={onEditPromptKeydown}
					/>
					<Button
						variant="outline"
						disabled={!connected || !editPromptDirty}
						onclick={applyEditPrompt}
					>
						{t('app.realtime_canvas.edit_prompt_apply')}
					</Button>
				</div>
				<p class="text-dim text-[11.5px]">{t('app.realtime_canvas.edit_prompt_hint')}</p>
			{/if}
			{#if selection}
				<p class="text-dim text-[11.5px]">{t('app.realtime_canvas.selection_hint')}</p>
			{:else}
				<p class="text-dim text-[11.5px]">{t('app.realtime_canvas.selecting_hint')}</p>
			{/if}
			<div class="flex flex-wrap gap-2">
				{#if selecting}
					<Button variant="outline" size="sm" disabled={!connected} onclick={selectWholeCanvas}>
						{t('app.realtime_canvas.select_all')}
					</Button>
				{/if}
				{#if selection}
					<Button variant="outline" size="sm" disabled={openingDrawing} onclick={eraseSelection}>
						{t('app.realtime_canvas.erase_selection')}
					</Button>
					<Button variant="ghost" size="sm" disabled={!connected} onclick={clearSelection}>
						{t('app.realtime_canvas.clear_selection')}
					</Button>
				{/if}
			</div>
		</div>
	{/if}
{/snippet}

{#snippet modelPicker()}
	{#if !studio.modelsLoaded}
		<div
			role="status"
			class="border-line-strong flex items-center gap-2.5 rounded-md border px-2.5 py-2"
		>
			<span class="sr-only">{t('app.models.loading')}</span>
			<Skeleton class="size-7 rounded-md" aria-hidden="true" />
			<div class="flex flex-1 flex-col gap-1.5" aria-hidden="true">
				<Skeleton class="h-3 w-28 rounded" />
				<Skeleton class="h-2.5 w-20 rounded" />
			</div>
		</div>
	{:else}
		<div
			class="bg-field border-line-strong focus-within:ring-ring/60 relative flex items-center gap-2.5 rounded-md border px-2.5 py-2 focus-within:ring-2"
		>
			<span
				class="bg-raised ring-line-strong grid size-7 shrink-0 place-items-center rounded-md text-xs font-semibold ring-1 ring-inset"
				aria-hidden="true">{selectedModel?.name.charAt(0) ?? '-'}</span
			>
			<div class="min-w-0 flex-1">
				<p class="truncate font-medium">
					{selectedModel?.name ?? t('app.realtime_canvas.no_model')}
				</p>
				<p class="text-dim text-[11px] tabular-nums">
					{CANVAS_SIZE} × {CANVAS_SIZE}{selectedModel?.realtime_p95_ms != null
						? ` · p95 ${Math.round(selectedModel.realtime_p95_ms)} ${t('app.realtime_canvas.latency_ms')}`
						: ''}
				</p>
			</div>
			{#if realtimeModels.length > 1}
				<ChevronsUpDownIcon class="text-dim size-3.5 shrink-0" aria-hidden="true" />
				<!-- The native select keeps keyboard and screen reader behaviour; it sits
			     invisibly over the card that shows the choice. -->
				<select
					id="realtime-model"
					bind:value={modelId}
					disabled={busy}
					class="absolute inset-0 cursor-pointer opacity-0 disabled:cursor-not-allowed"
				>
					{#each realtimeModels as model (model.id)}
						<option value={model.id}>{modelOptionLabel(model)}</option>
					{/each}
				</select>
			{/if}
		</div>
	{/if}
{/snippet}

<div
	class="rt relative flex min-h-0 flex-1 flex-col overflow-y-auto lg:flex-row lg:overflow-hidden"
	data-mode={focus ? 'focus' : 'workbench'}
	bind:this={panelRoot}
>
	<div class="stage relative flex min-w-0 flex-col lg:min-h-0 lg:flex-1">
		<div class="stage-body">
			<div class="rail-slot">{@render toolRail()}</div>
			<div class="panes">
				<section class="pane pane-sketch" aria-label={t('app.realtime_canvas.input_title')}>
					<div class="pane-head">
						<h2 class="text-[12.5px] font-semibold">{t('app.realtime_canvas.sketch_title')}</h2>
						<span class="text-dim text-[11.5px] tabular-nums">{CANVAS_SIZE} × {CANVAS_SIZE}</span>
						<span class="flex-1"></span>
						{#snippet undoButton(props: Record<string, unknown>)}
							<button
								{...props}
								type="button"
								class="icon-button hit-slop"
								aria-keyshortcuts={ariaKeyShortcuts(UNDO_KEYS)}
								disabled={openingDrawing || !canUndo}
								onclick={undoCanvas}
								><Undo2Icon class="size-[15px]" aria-hidden="true" /><span class="sr-only"
									>{t('app.realtime_canvas.undo')}</span
								></button
							>
						{/snippet}
						{@render hinted(t('app.realtime_canvas.undo'), UNDO_KEYS, 'top', undoButton)}
						{#snippet redoButton(props: Record<string, unknown>)}
							<button
								{...props}
								type="button"
								class="icon-button hit-slop"
								aria-keyshortcuts={ariaKeyShortcuts(REDO_KEYS)}
								disabled={openingDrawing || !canRedo}
								onclick={redoCanvas}
								><Redo2Icon class="size-[15px]" aria-hidden="true" /><span class="sr-only"
									>{t('app.realtime_canvas.redo')}</span
								></button
							>
						{/snippet}
						{@render hinted(t('app.realtime_canvas.redo'), REDO_KEYS, 'top', redoButton)}
						<button
							type="button"
							class="icon-button hit-slop"
							title={t('app.realtime_canvas.clear')}
							disabled={openingDrawing || blank}
							onclick={clearCanvas}
							><Trash2Icon class="size-[15px]" aria-hidden="true" /><span class="sr-only"
								>{t('app.realtime_canvas.clear')}</span
							></button
						>
					</div>
					<div
						class="surface surface-sketch"
						role="img"
						aria-label={t('app.realtime_canvas.draw_surface')}
						style:--sketch-opacity={sketchOpacity / 100}
					>
						<canvas
							bind:this={drawCanvas}
							width={CANVAS_SIZE}
							height={CANVAS_SIZE}
							aria-label={t('app.realtime_canvas.draw_surface')}
							class="block h-full w-full touch-none bg-white"
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
								class="pointer-events-none absolute inset-0 h-full w-full"
							></canvas>
						{/if}
					</div>
					{@render paint()}
				</section>

				<section class="pane pane-output" aria-label={t('app.realtime_canvas.output_title')}>
					<div class="pane-head">
						<h2 class="text-[12.5px] font-semibold">{t('app.realtime_canvas.output_title')}</h2>
						<span class="text-dim text-[11.5px] tabular-nums" title={frameCounterTitle}
							>{t('app.realtime_canvas.output_frame').replace(
								'{count}',
								renderedFrames.toLocaleString()
							)}</span
						>
						<span class="flex-1"></span>
						<button
							type="button"
							class="icon-button hit-slop"
							aria-label={t('app.realtime_canvas.capture')}
							title={t('app.realtime_canvas.capture')}
							disabled={renderedFrames === 0}
							onclick={captureOutput}><CameraIcon class="size-[15px]" aria-hidden="true" /></button
						>
						<button
							type="button"
							class="icon-button hit-slop"
							aria-label={t('app.realtime_canvas.download_frame')}
							title={t('app.realtime_canvas.download_frame')}
							disabled={renderedFrames === 0}
							onclick={downloadOutput}
							><DownloadIcon class="size-[15px]" aria-hidden="true" /></button
						>
						<button
							type="button"
							class="icon-button hit-slop focus-entry"
							aria-label={t('app.realtime_canvas.view_focus')}
							title={t('app.realtime_canvas.view_focus')}
							onclick={() => setViewMode('focus')}
							><Maximize2Icon class="size-[15px]" aria-hidden="true" /></button
						>
					</div>
					<div
						class="surface surface-output"
						role="img"
						aria-label={t('app.realtime_canvas.output_title')}
					>
						<canvas
							bind:this={outputCanvas}
							width={CANVAS_SIZE}
							height={CANVAS_SIZE}
							class="bg-raised block h-full w-full"
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
								class="pointer-events-none absolute inset-0 h-full w-full"
							></canvas>
						{/if}
						{#if renderedFrames === 0}
							<p
								class="text-muted-foreground pointer-events-none absolute inset-0 grid place-items-center px-6 text-center text-[13px]"
							>
								{t('app.realtime_canvas.output_empty')}
							</p>
						{/if}
					</div>
					<div class="pane-foot text-dim flex items-center gap-2 text-[11.5px]">
						<ZapIcon class="size-[13px] shrink-0" aria-hidden="true" />
						<span class="truncate"
							>{busy
								? t('app.realtime_canvas.prompt_locked')
								: t('app.realtime_canvas.output_sub')}</span
						>
					</div>
				</section>
			</div>

			{#if focus}
				<div class="opacity-control glass">
					<label for="realtime-sketch-opacity" class="text-muted-foreground"
						>{t('app.realtime_canvas.sketch_opacity')}</label
					>
					<input
						id="realtime-sketch-opacity"
						type="range"
						min="0"
						max="100"
						step="1"
						bind:value={sketchOpacity}
						class="accent-foreground w-24"
					/>
					<span class="w-8 text-end tabular-nums">{sketchOpacity}%</span>
				</div>
				{#if supportsSelection && (selecting || selection)}
					<div class="selection-float glass">{@render selectionControls()}</div>
				{/if}
				<div class="prompt-bar glass">
					<span
						class="border-line-strong text-foreground flex h-[30px] max-w-44 shrink-0 items-center gap-2 rounded-full border px-2.5 text-xs font-medium"
					>
						<span
							class="bg-raised ring-line-strong size-3.5 rounded ring-1 ring-inset"
							aria-hidden="true"
						></span>
						<span class="truncate">{selectedModel?.name ?? t('app.realtime_canvas.no_model')}</span>
					</span>
					<Label for="realtime-prompt" class="sr-only">{t('app.gen.prompt')}</Label>
					<input
						id="realtime-prompt"
						name="prompt"
						autocomplete="off"
						bind:value={prompt}
						placeholder={t('app.realtime_canvas.prompt_placeholder')}
						class="placeholder:text-dim h-[34px] min-w-0 flex-1 bg-transparent px-2.5 text-[13.5px] outline-none"
						onkeydown={onPromptKeydown}
					/>
					{#if selectedModel}
						<span class="pill tabular-nums"
							>{t('app.realtime_canvas.structure')}
							{formatParamValue(structureValue, structureRange)}</span
						>
						<span class="pill tabular-nums"
							>{t('app.realtime_canvas.steps')} {formatParamValue(stepsValue, stepsRange)}</span
						>
						<button
							type="button"
							class="icon-button hit-slop bordered"
							aria-label={t('app.realtime_canvas.guidance')}
							aria-expanded={guidanceOpen}
							aria-controls="realtime-guidance-float"
							onclick={() => (guidanceOpen = !guidanceOpen)}
							><SlidersHorizontalIcon class="size-[15px]" aria-hidden="true" /></button
						>
					{/if}
					{#if connected}
						<Button variant="brand" disabled={!promptDirty} onclick={applyPrompt}>
							{t('app.realtime_canvas.update')}
							<kbd class="kbd-on-brand">↵</kbd>
						</Button>
					{:else}
						<Button variant="brand" disabled={!canConnect} onclick={connect}>
							{t('app.realtime_canvas.connect')}
							<kbd class="kbd-on-brand">↵</kbd>
						</Button>
					{/if}
				</div>
				{#if guidanceOpen && selectedModel}
					<div id="realtime-guidance-float" class="guidance-float glass">
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
			{/if}
		</div>

		<div class="bottom-bar">
			<div class="flex min-w-0 flex-1 items-center gap-2.5">
				<span class="text-muted-foreground w-16 shrink-0 text-xs font-medium"
					>{t('app.realtime_canvas.captures')}</span
				>
				<div class="no-scrollbar flex min-w-0 gap-2 overflow-x-auto p-0.5">
					{#each captures as capture, index (capture.id)}
						<button
							type="button"
							class="capture"
							aria-label={t('app.realtime_canvas.capture_download').replace(
								'{index}',
								String(index + 1)
							)}
							title={t('app.realtime_canvas.capture_download').replace(
								'{index}',
								String(index + 1)
							)}
							onclick={() => downloadUrl(capture.url, `potocolom-capture-${capture.id}.png`)}
						>
							<img src={capture.url} alt="" width="52" height="52" />
						</button>
					{/each}
					<button
						type="button"
						class="capture capture-add"
						aria-label={t('app.realtime_canvas.capture')}
						title={t('app.realtime_canvas.capture')}
						disabled={renderedFrames === 0}
						onclick={captureOutput}><CameraIcon class="size-4" aria-hidden="true" /></button
					>
				</div>
			</div>
			<div class="flex min-w-0 flex-col items-end gap-1">
				<div role="status" class="text-end">
					{#if drawingNotice}
						<p class="text-destructive text-xs" aria-live="polite">{t(drawingNotice)}</p>
					{/if}
				</div>
				<div class="flex items-center gap-2">
					<Button variant="outline" disabled={openingDrawing} onclick={saveDrawing}>
						<SaveIcon />
						{t('app.realtime_canvas.save')}
					</Button>
					<Button
						variant="outline"
						disabled={openingDrawing}
						aria-describedby="realtime-open-hint"
						onclick={chooseDrawingFile}
					>
						<FolderOpenIcon />
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
				<p id="realtime-open-hint" class="text-dim max-w-80 text-end text-[11px]">
					{t('app.realtime_canvas.open_hint')}
				</p>
			</div>
		</div>
	</div>

	{#if !focus}
		<aside class="inspector">
			<div class="section">
				<h2 class="section-title">{t('app.realtime_canvas.model')}</h2>
				{#if realtimeModels.length > 1}
					<Label for="realtime-model" class="sr-only">{t('app.realtime_canvas.model')}</Label>
				{/if}
				{@render modelPicker()}
				{#if selectedModel}
					<div class="mt-2 flex flex-wrap gap-1.5">
						<span class="tag">{t('app.realtime_canvas.cap_realtime')}</span>
						{#if supportsSelection}<span class="tag">{t('app.realtime_canvas.cap_masks')}</span
							>{/if}
						{#if supportsEditPrompt}<span class="tag"
								>{t('app.realtime_canvas.cap_edit_prompt')}</span
							>{/if}
					</div>
				{/if}
				{#if selectedModel?.requires_attribution}
					<p class="text-dim mt-2 text-xs">{selectedModel.requires_attribution}</p>
				{/if}
				{#if modelId === '' && studio.modelsLoaded}
					<p class="text-muted-foreground mt-2 text-[13px]">{t('app.realtime_canvas.no_model')}</p>
				{/if}
			</div>
			<div class="section">
				<div class="section-title">
					<Label for="realtime-prompt" class="text-xs font-semibold">{t('app.gen.prompt')}</Label>
					<span class="text-dim font-medium">{t('app.realtime_canvas.prompt_next_frame')}</span>
				</div>
				<textarea
					id="realtime-prompt"
					name="prompt"
					autocomplete="off"
					rows="3"
					bind:value={prompt}
					placeholder={t('app.realtime_canvas.prompt_placeholder')}
					aria-describedby="realtime-prompt-hint"
					class="bg-field border-line-strong placeholder:text-dim focus-visible:ring-ring/60 min-h-[76px] w-full resize-none rounded-md border px-2.5 py-2 text-[13px] leading-normal outline-none focus-visible:ring-2"
					onkeydown={onPromptKeydown}></textarea>
				<div class="mt-2 flex items-center gap-2">
					<span class="text-dim text-[11px] tabular-nums">{tokenLabel}</span>
					<span class="flex-1"></span>
					<Button variant="outline" size="sm" disabled={!promptDirty} onclick={applyPrompt}>
						{t('app.realtime_canvas.update')}
						<kbd class="kbd">{shortcutLabel(['mod', 'enter'])}</kbd>
					</Button>
				</div>
				<p id="realtime-prompt-hint" class="text-dim mt-1.5 text-[11px]">
					{busy ? t('app.realtime_canvas.prompt_locked') : t('app.realtime_canvas.prompt_required')}
				</p>
			</div>
			{#if selectedModel}
				<div class="section">
					<h2 class="section-title">{t('app.realtime_canvas.guidance')}</h2>
					<div class="flex flex-col gap-3.5">
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
				</div>
			{/if}
			{#if supportsSelection && (selecting || selection)}
				<div class="section bg-brand/5">{@render selectionControls()}</div>
			{/if}
			<div class="section section-last">
				<div class="section-title">
					<span>{t('app.realtime_canvas.session')}</span>
					{#if sessionElapsed !== null}
						<span class="text-dim font-medium tabular-nums">{sessionElapsed}</span>
					{/if}
				</div>
				<dl class="grid grid-cols-3 gap-2" title={frameCounterTitle}>
					<div>
						<dt class="text-dim text-[11px]">{t('app.realtime_canvas.stat_sent')}</dt>
						<dd class="mt-0.5 text-sm tabular-nums">{sentFrames.toLocaleString()}</dd>
					</div>
					<div>
						<dt class="text-dim text-[11px]">{t('app.realtime_canvas.stat_rendered')}</dt>
						<dd class="mt-0.5 text-sm tabular-nums">{renderedFrames.toLocaleString()}</dd>
					</div>
					<div>
						<dt class="text-dim text-[11px]">{t('app.realtime_canvas.stat_skipped')}</dt>
						<dd class="mt-0.5 text-sm tabular-nums">{stageCounters.replaced.toLocaleString()}</dd>
					</div>
				</dl>
			</div>
			<div role="status" class="px-4 pb-4">
				{#if notice}
					<p class="text-destructive text-[13px]" aria-live="polite">
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
			</div>
		</aside>
	{:else}
		<div role="status" class="session-notice">
			{#if notice}
				<p class="text-destructive text-[13px]" aria-live="polite">
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
		</div>
	{/if}
</div>

<style>
	.stage-body {
		display: flex;
		flex-direction: column;
		gap: 0.875rem;
		padding: 1rem;
	}
	.rail {
		display: flex;
		align-items: center;
		gap: 2px;
		padding: 6px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--panel);
		width: fit-content;
	}
	/* Hit areas: the rail packs its buttons 2px apart, so theirs grow across
	   the rail only; the head's icons and the swatches sit 8px apart and
	   reach half of that toward each other (see .hit-slop in app.css). */
	.rail-button,
	.icon-button,
	.swatch {
		position: relative;
	}
	.rail-button {
		--hit-slop-x: 1px;
	}
	.icon-button,
	.swatch {
		--hit-slop-x: 4px;
	}
	.rail-button,
	.icon-button {
		display: grid;
		place-items: center;
		border-radius: 6px;
		color: var(--muted-foreground);
		outline: none;
	}
	.rail-button {
		width: 34px;
		height: 34px;
	}
	.icon-button {
		width: 30px;
		height: 30px;
	}
	.icon-button.bordered {
		border: 1px solid var(--line-strong);
		background: var(--raised);
	}
	/* Scoped rules win over Tailwind's layered utilities, so responsive display
	   and the last section's border are set here rather than with utilities. */
	.icon-button.focus-entry {
		display: none;
	}
	@media (min-width: 1024px) {
		.icon-button.focus-entry {
			display: grid;
		}
	}
	.rail-button:hover:not(:disabled),
	.icon-button:hover:not(:disabled) {
		background: var(--hover);
		color: var(--foreground);
	}
	.rail-button[aria-pressed='true'] {
		background: var(--brand-soft);
		color: #6e96f8;
	}
	.rail-button:focus-visible,
	.icon-button:focus-visible,
	.swatch:focus-visible,
	.capture:focus-visible {
		box-shadow: 0 0 0 2px var(--ring);
	}
	.rail-button:disabled,
	.icon-button:disabled {
		opacity: 0.4;
	}
	.rail-divider {
		width: 1px;
		height: 20px;
		margin: 0 4px;
		background: var(--line-strong);
	}
	.panes {
		display: grid;
		gap: 0.875rem;
		min-width: 0;
	}
	.pane {
		display: flex;
		min-width: 0;
		flex-direction: column;
	}
	.pane-head {
		display: flex;
		height: 38px;
		align-items: center;
		gap: 8px;
		padding: 0 4px;
	}
	.surface {
		position: relative;
		width: 100%;
		max-width: 100%;
		aspect-ratio: 1;
		overflow: hidden;
		border-radius: 8px;
		box-shadow: 0 0 0 1px var(--line-strong);
	}
	.pane-foot {
		height: 30px;
		margin-top: 12px;
		padding: 0 2px;
	}
	.paint {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 12px 14px;
		margin-top: 12px;
		padding: 0 2px;
	}
	.swatches {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	.swatch {
		width: 18px;
		height: 18px;
		border-radius: 999px;
		box-shadow: inset 0 0 0 1px rgb(255 255 255 / 15%);
		outline: none;
	}
	.swatch[aria-pressed='true'] {
		box-shadow:
			0 0 0 2px var(--background),
			0 0 0 3.5px #fff;
	}
	.size {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-inline-start: auto;
	}
	.size :global(.size-slider) {
		width: 72px;
	}
	.size-unit {
		display: none;
	}
	.bottom-bar {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 10px 16px;
		border-top: 1px solid var(--border);
		padding: 10px 16px;
	}
	.capture {
		position: relative;
		width: 52px;
		height: 52px;
		flex: none;
		overflow: hidden;
		border-radius: 6px;
		box-shadow: 0 0 0 1px var(--line-strong);
		outline: none;
	}
	.capture img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.capture-add {
		display: grid;
		place-items: center;
		border: 1px dashed var(--line-strong);
		box-shadow: none;
		color: var(--dim);
	}
	.capture-add:disabled {
		opacity: 0.5;
	}
	.inspector {
		display: flex;
		flex-direction: column;
		border-top: 1px solid var(--border);
		background: var(--panel);
	}
	.section {
		border-bottom: 1px solid var(--border);
		padding: 14px 16px;
	}
	.section.section-last {
		border-bottom: 0;
	}
	.section-title {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-bottom: 12px;
		font-size: 12px;
		font-weight: 600;
	}
	.pill {
		display: inline-flex;
		height: 30px;
		flex: none;
		align-items: center;
		border: 1px solid var(--line-strong);
		border-radius: 999px;
		padding: 0 9px;
		color: var(--muted-foreground);
		font-family: var(--font-mono);
		font-size: 11.5px;
		white-space: nowrap;
	}
	.kbd-on-brand {
		border: 1px solid rgb(255 255 255 / 30%);
		border-radius: 4px;
		padding: 0 4px;
		color: #dfe6ff;
		font: 500 11px var(--font-sans);
		line-height: 15px;
	}
	.glass {
		border: 1px solid var(--line-strong);
		border-radius: 10px;
		background: rgb(15 15 15 / 92%);
		box-shadow: 0 12px 40px rgb(0 0 0 / 45%);
	}

	@media (min-width: 1024px) {
		.stage-body {
			flex: 1;
			min-height: 0;
			flex-direction: row;
			align-items: center;
			padding: 0 16px;
		}
		.rail {
			flex-direction: column;
			margin-top: -8px;
		}
		.rail-button {
			--hit-slop-x: 40px;
			--hit-slop-y: 1px;
		}
		.rail-divider {
			width: 20px;
			height: 1px;
			margin: 4px 0;
		}
		.panes {
			flex: 1;
			grid-template-columns: repeat(2, minmax(0, 1fr));
			align-items: start;
		}
		.surface {
			width: min(100%, calc(100svh - 262px));
		}
		.bottom-bar {
			flex-wrap: nowrap;
		}
		.inspector {
			width: 312px;
			flex: none;
			overflow-y: auto;
			border-top: 0;
			border-inline-start: 1px solid var(--border);
		}

		/* Focus: one large output with the sketch laid over it. */
		.rt[data-mode='focus'] .stage {
			background: radial-gradient(ellipse at 50% 45%, #121212 0%, #0a0a0a 70%);
		}
		.rt[data-mode='focus'] .stage-body {
			--focus-size: min(calc(100vw - 420px), calc(100svh - 290px));
			position: relative;
			justify-content: center;
			padding: 20px 24px 92px;
		}
		.rt[data-mode='focus'] .rail-slot {
			position: absolute;
			top: 50%;
			left: 24px;
			z-index: 20;
			transform: translateY(-50%);
		}
		.rt[data-mode='focus'] .rail {
			margin: 0;
			background: rgb(15 15 15 / 92%);
			border-color: var(--line-strong);
			box-shadow: 0 12px 40px rgb(0 0 0 / 45%);
		}
		.rt[data-mode='focus'] .panes {
			flex: none;
			grid-template-columns: 1fr;
			grid-template-areas: 'stack';
		}
		.rt[data-mode='focus'] .pane {
			grid-area: stack;
		}
		.rt[data-mode='focus'] .pane-head,
		.rt[data-mode='focus'] .pane-foot {
			display: none;
		}

		.rt[data-mode='focus'] .surface {
			width: var(--focus-size);
			border-radius: 12px;
		}
		.rt[data-mode='focus'] .surface-output {
			box-shadow:
				0 0 0 1px var(--line-strong),
				0 30px 80px rgb(0 0 0 / 60%);
		}
		.rt[data-mode='focus'] .surface-sketch {
			/* On the surface itself, not the pane: a stacking context on the pane
			   would isolate the blend from the frame underneath. */
			z-index: 1;
			box-shadow: none;
			opacity: var(--sketch-opacity);
			mix-blend-mode: multiply;
		}
		.rt[data-mode='focus'] .paint {
			position: absolute;
			top: 50%;
			right: 24px;
			z-index: 20;
			flex-direction: column;
			margin: 0;
			padding: 10px;
			transform: translateY(-50%);
			border: 1px solid var(--line-strong);
			border-radius: 10px;
			background: rgb(15 15 15 / 92%);
			box-shadow: 0 12px 40px rgb(0 0 0 / 45%);
		}
		.rt[data-mode='focus'] .swatches {
			flex-direction: column;
			gap: 10px;
		}
		.rt[data-mode='focus'] .swatch {
			--hit-slop-x: 40px;
			--hit-slop-y: 5px;
		}
		.rt[data-mode='focus'] .size {
			flex-direction: column;
			margin: 0;
			gap: 8px;
		}
		.rt[data-mode='focus'] .size :global(.size-slider) {
			width: 64px;
		}
		.rt[data-mode='focus'] .size-unit {
			display: inline;
		}
		.opacity-control {
			position: absolute;
			/* The output's top right corner: the frame is centred in the padded
			   stage, 20px above and 92px below. */
			top: calc(20px + (100% - 112px - var(--focus-size)) / 2 + 12px);
			left: calc(50% + var(--focus-size) / 2 - 12px);
			transform: translateX(-100%);
			z-index: 20;
			display: flex;
			align-items: center;
			gap: 10px;
			padding: 8px 10px;
			font-size: 12px;
		}
		.selection-float {
			position: absolute;
			top: 20px;
			left: 24px;
			z-index: 20;
			width: 300px;
			padding: 12px;
		}
		.prompt-bar {
			position: absolute;
			bottom: 18px;
			left: 50%;
			z-index: 20;
			display: flex;
			width: min(760px, calc(100% - 48px));
			align-items: center;
			gap: 8px;
			padding: 8px;
			transform: translateX(-50%);
		}
		.guidance-float {
			position: absolute;
			bottom: 80px;
			left: 50%;
			z-index: 20;
			display: flex;
			width: 300px;
			flex-direction: column;
			gap: 14px;
			padding: 14px;
			transform: translateX(calc(-50% + 190px));
		}
		.session-notice {
			position: absolute;
			top: 64px;
			left: 50%;
			z-index: 30;
			transform: translateX(-50%);
		}
	}
</style>
