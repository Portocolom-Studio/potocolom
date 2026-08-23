<script lang="ts">
	// The live realtime drawing surface (issue #3). One 512 by 512 bitmap, CSS
	// scaled for display, sent as complete WebP frames over the realtime
	// protocol. The framing, the send predicate, the cadence and the opening
	// message live in $lib/realtime-canvas so they carry tests. What stays here
	// is not only the DOM: the timer, the changed and encoding flags, the
	// latest-wins drop policy, the decode loop and the session lifecycle live in
	// this file. The happy path is covered by a browser run; the failure and
	// teardown paths (refused sessions, invalid session ids, overlapping
	// sockets, stale asynchronous work) are not covered by any automated test.
	//
	// Out of scope here, by their own issues: the replayable stroke journal and
	// undo (#54), frame-diff skip and finer cadence adaptation (#42), and
	// monotonic input revisions with generated-output correlation (#19). The
	// shipped 17 byte header carries no sequence number, so generated frames
	// are presented in transport order.
	import { t } from '$lib/i18n.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
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
		FAST_INTERVAL_MS,
		IDLE_TICKS_BEFORE_STOP,
		afterParamsUpdated,
		canvasFrame,
		nextDelayMs,
		openMessage,
		parseGeneratedFrame,
		shouldSendFrame,
		stateForCloseCode,
		updateParamsMessage,
		type ConnectionState
	} from '$lib/realtime-canvas';

	/** The wire dimensions. CSS scales the display without changing these. */
	const CANVAS_SIZE = 512;
	const STROKE_WIDTH = 6;
	const INK = '#111827';
	const PAPER = '#ffffff';
	// The shape uuidBytes requires: 32 hex digits with optional hyphens in the
	// 8-4-4-4-12 positions.
	const UUID_RE = /^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$/i;

	let drawCanvas = $state<HTMLCanvasElement | undefined>();
	let outputCanvas = $state<HTMLCanvasElement | undefined>();

	/** A message is held as its key, not its text, so switching language
	 * retranslates it instead of leaving the previous locale on screen. */
	type NoticeKey = Parameters<typeof t>[0];

	let prompt = $state('');
	let connection = $state<ConnectionState>('idle');
	let notice = $state<NoticeKey | ''>('');
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

	let socket: WebSocket | null = null;
	let sessionId: string | null = null;
	let paint: CanvasRenderingContext2D | null = null;

	let changed = false; // paint not yet on the wire
	let encoding = false; // one encode in flight
	let decoding = false; // one decode in flight
	// Latest generated frame, older ones dropped. The buffer is named in the
	// type so the Blob constructor accepts these bytes without a cast.
	let pendingFrame: Uint8Array<ArrayBuffer> | null = null;
	let timer: ReturnType<typeof setTimeout> | null = null;
	let idleTicks = 0;
	let lastFrameCostMs = 0;
	let blank = true; // nothing drawn since the last clear
	let drawing = false;
	let strokePointer: number | null = null;
	let lastPoint: { x: number; y: number } | null = null;
	let userClosing = false;
	// Slider updates are debounced so a drag does not send one message per
	// pixel: the timer is re-armed on every movement and fires once the slider
	// has settled, mirroring how armCapture/stopTimer time the capture loop.
	const SLIDER_UPDATE_MS = 300;
	let structureTimer: ReturnType<typeof setTimeout> | null = null;
	let stepsTimer: ReturnType<typeof setTimeout> | null = null;

	// The tool in use on the canvas. A forward contract for issue #54's
	// replayable stroke journal: for its replay to stay faithful, erase must be
	// recorded as a stroke operation carrying a mode flag, not as a separate
	// kind of event.
	let tool = $state<'draw' | 'erase'>('draw');
	// Erase paints the paper back on: the canvas stays opaque, because a
	// transparent pixel would reach the model as transparency rather than as
	// blank paper, and the conditioning path assumes dark strokes on white.
	const strokeColour = $derived(tool === 'erase' ? PAPER : INK);

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
	// Frames only go out while a worker is actually attached. During a reassign
	// the socket is open and the session is alive, but the API has no worker to
	// relay to and silently drops whatever arrives.
	const sending = $derived(connection === 'active');
	const busy = $derived(connection === 'connecting' || connected);
	const canConnect = $derived(!busy && modelId !== '' && prompt.trim() !== '');
	// The prompt differs from the last one the API confirmed; whitespace around
	// it does not count, because openMessage and updateParamsMessage both trim.
	const promptDirty = $derived(connected && prompt.trim() !== appliedPrompt);

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
			sendUpdate({ structure_strength: structureValue });
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
			sendUpdate({ steps: stepsValue });
		}, SLIDER_UPDATE_MS);
	});

	$effect(() => {
		if (!drawCanvas) return;
		paint = drawCanvas.getContext('2d');
		if (!paint) return;
		// Opaque white: a transparent canvas would reach the model as
		// transparency rather than as the blank paper the user sees.
		paint.fillStyle = PAPER;
		paint.fillRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);
		paint.lineWidth = STROKE_WIDTH;
		paint.lineCap = 'round';
		paint.lineJoin = 'round';
		paint.strokeStyle = INK;
	});

	// Tear the socket and the timer down with the panel, so leaving the view
	// does not leave a session open on a worker.
	$effect(() => () => {
		userClosing = true;
		stopTimer();
		if (structureTimer !== null) clearTimeout(structureTimer);
		if (stepsTimer !== null) clearTimeout(stepsTimer);
		socket?.close(1000);
		socket = null;
	});

	function canvasPoint(event: PointerEvent): { x: number; y: number } {
		const canvas = event.currentTarget as HTMLCanvasElement;
		const rect = canvas.getBoundingClientRect();
		return {
			x: ((event.clientX - rect.left) / rect.width) * CANVAS_SIZE,
			y: ((event.clientY - rect.top) / rect.height) * CANVAS_SIZE
		};
	}

	function markChanged(): void {
		changed = true;
		blank = false;
		idleTicks = 0;
		// The loop stops arming itself once the canvas goes quiet, so new paint
		// has to restart it.
		if (sending && timer === null) armCapture(FAST_INTERVAL_MS);
	}

	/** One segment per move, so a long stroke does not restroke its whole path. */
	function drawSegment(from: { x: number; y: number }, to: { x: number; y: number }): void {
		if (!paint) return;
		paint.strokeStyle = strokeColour;
		paint.beginPath();
		paint.moveTo(from.x, from.y);
		paint.lineTo(to.x, to.y);
		paint.stroke();
	}

	/**
	 * A tap is filled rather than stroked, so the mark does not depend on how an
	 * engine treats a zero-length segment. Measured in Chrome 149: stroking from
	 * a point to itself with a round cap does paint a dot, 32 dark pixels, the
	 * same as this arc; with a butt cap it paints nothing, and a lone moveTo with
	 * no segment paints nothing either. The spec prunes zero-length segments and
	 * calls a one-point path empty, so the round-cap dot is engine behaviour to
	 * lean on rather than a guarantee. Filling says what is meant.
	 */
	function drawDot(at: { x: number; y: number }): void {
		if (!paint) return;
		paint.beginPath();
		paint.arc(at.x, at.y, STROKE_WIDTH / 2, 0, Math.PI * 2);
		paint.fillStyle = strokeColour;
		paint.fill();
	}

	function onPointerDown(event: PointerEvent): void {
		if (!event.isPrimary || !paint) return;
		drawing = true;
		strokePointer = event.pointerId;
		(event.currentTarget as HTMLCanvasElement).setPointerCapture(event.pointerId);
		const point = canvasPoint(event);
		// A tap that never moves should still leave a mark.
		drawDot(point);
		lastPoint = point;
		markChanged();
	}

	function onPointerMove(event: PointerEvent): void {
		// isPrimary and the stroke's own pointer id: without both, a plain hover
		// after a keyboard-driven pen down would draw, and a second finger would
		// append its moves to the first finger's stroke.
		if (!drawing || !event.isPrimary || event.pointerId !== strokePointer) return;
		if (!lastPoint) return;
		const point = canvasPoint(event);
		drawSegment(lastPoint, point);
		lastPoint = point;
		markChanged();
	}

	function onPointerUp(event: PointerEvent): void {
		// The stroke's own pointer id only: another pointer's release must not
		// end this stroke.
		if (event.pointerId !== strokePointer) return;
		drawing = false;
		strokePointer = null;
		lastPoint = null;
	}

	function clearCanvas(): void {
		// A blank canvas has nothing to clear, and sending it again would spend
		// an inference reproducing what the model already returned.
		if (!paint || blank) return;
		paint.fillStyle = PAPER;
		paint.fillRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);
		blank = true;
		// Still a revision worth sending: the model should stop drawing what is
		// no longer on the canvas.
		changed = true;
		idleTicks = 0;
		if (sending && timer === null) armCapture(FAST_INTERVAL_MS);
	}

	function stopTimer(): void {
		if (timer !== null) clearTimeout(timer);
		timer = null;
	}

	/**
	 * Send an update_params message for a subset of the session's params.
	 * Only while the session is live and the socket is open: everything else
	 * silently drops, and the params_updated confirmation records what the
	 * API actually merged rather than what was sent.
	 */
	function sendUpdate(params: Record<string, string | number>): void {
		if (!connected || !socket || socket.readyState !== WebSocket.OPEN) return;
		socket.send(updateParamsMessage(params));
	}

	function applyPrompt(): void {
		if (!promptDirty) return;
		// updateParamsMessage trims, so the untrimmed text is sent.
		sendUpdate({ prompt });
	}

	function armCapture(delay: number): void {
		stopTimer();
		timer = setTimeout(() => void captureTick(), delay);
	}

	function encodeCanvas(canvas: HTMLCanvasElement): Promise<Uint8Array> {
		return new Promise((resolve, reject) => {
			// WebP is what docs/connection-handling.md specifies. A browser that
			// cannot encode WebP silently returns PNG instead, which the worker's
			// decoder happens to open too, so this leans on that rather than
			// failing: an extension to the documented wire, not conformance.
			canvas.toBlob((blob) => {
				if (!blob) {
					reject(new Error('the canvas produced no image'));
					return;
				}
				blob.arrayBuffer().then((buffer) => resolve(new Uint8Array(buffer)), reject);
			}, 'image/webp');
		});
	}

	async function captureTick(): Promise<void> {
		timer = null;
		if (!socket || socket.readyState !== WebSocket.OPEN || !sessionId || !drawCanvas) return;
		if (!sending) return; // a reassign is in flight; resumed re-arms

		if (!shouldSendFrame({ changed, encoding, buffered: socket.bufferedAmount })) {
			idleTicks += 1;
			// Nothing to send for a while: stop arming rather than hold a timer
			// open on an untouched canvas. Paint, a clear, or a resume restarts it.
			if (idleTicks >= IDLE_TICKS_BEFORE_STOP && !changed) return;
			armCapture(nextDelayMs(lastFrameCostMs));
			return;
		}

		// The session this frame belongs to. Encoding is asynchronous, so by the
		// time it finishes the user may have disconnected, cleared and
		// reconnected; sending then would frame a stale bitmap as the new
		// session and hand the replacement worker input the canvas no longer has.
		const forSocket = socket;
		const forSession = sessionId;
		const started = performance.now();
		// Cleared before the encode, so paint arriving during it marks the canvas
		// changed again rather than being swallowed by this frame.
		changed = false;
		encoding = true;
		try {
			const image = await encodeCanvas(drawCanvas);
			if (
				socket === forSocket &&
				sessionId === forSession &&
				forSocket.readyState === WebSocket.OPEN
			) {
				if (sending) {
					forSocket.send(canvasFrame(forSession, image));
					sentFrames += 1;
				} else {
					// An interrupted arrived while this encode was in flight: the
					// API has no worker to relay to and would drop the frame. Keep
					// the revision pending so the resumed handler re-sends it.
					changed = true;
				}
			}
		} catch {
			// A failed encode must not end the session silently: keep the
			// revision pending and let the next tick try again. A rejection
			// that outlived its session belongs to a session that is gone and
			// must not write into the one that replaced it.
			if (socket === forSocket && sessionId === forSession) {
				changed = true;
				notice = 'app.realtime_canvas.encode_failed';
			}
		} finally {
			encoding = false;
			lastFrameCostMs = performance.now() - started;
		}
		// Not after the session went away: re-arming there would resurrect a
		// timer the teardown had already stopped.
		if (socket === forSocket && sessionId === forSession) armCapture(nextDelayMs(lastFrameCostMs));
	}

	async function drainGenerated(): Promise<void> {
		if (decoding) return;
		decoding = true;
		try {
			while (pendingFrame !== null) {
				const image = pendingFrame;
				pendingFrame = null;
				const forSession = sessionId;
				// Per frame, so one that fails to decode does not strand the frame
				// waiting behind it: without this the loop exits and nothing runs
				// again until a further frame happens to arrive.
				try {
					// The Blob copies the bytes, so the socket's buffer is free to go.
					const bitmap = await createImageBitmap(new Blob([image], { type: 'image/webp' }));
					try {
						// A decode that outlived its session must not paint over the
						// output of the one that replaced it.
						const target = sessionId === forSession ? outputCanvas?.getContext('2d') : null;
						if (target) {
							target.drawImage(bitmap, 0, 0, CANVAS_SIZE, CANVAS_SIZE);
							renderedFrames += 1;
						}
					} finally {
						bitmap.close();
					}
				} catch {
					// A decode that failed for a session that is gone must not
					// write its notice into the session that replaced it.
					if (sessionId === forSession) {
						notice = 'app.realtime_canvas.decode_failed';
					}
				}
			}
		} catch {
			notice = 'app.realtime_canvas.decode_failed';
		} finally {
			decoding = false;
		}
	}

	function handleControl(text: string): void {
		let control: {
			type?: string;
			session_id?: string;
			code?: number;
			params?: unknown;
		};
		try {
			control = JSON.parse(text) as typeof control;
		} catch {
			return;
		}
		if (control.type === 'ready' && control.session_id) {
			// A server that sends a session id this client cannot frame is a
			// server this client cannot talk to. Failing visibly beats throwing
			// once per frame behind an Active badge.
			if (!UUID_RE.test(control.session_id)) {
				notice = 'app.realtime_canvas.socket_error';
				connection = 'failed';
				// Close the refused socket the way disconnect does, so no handler
				// of the refused session survives. onclose ignores a socket that is
				// no longer the module's, so clearing socket here keeps the
				// failure state just set above instead of letting onclose
				// overwrite it.
				stopTimer();
				socket?.close(1000);
				socket = null;
				sessionId = null;
				pendingFrame = null;
				return;
			}
			sessionId = control.session_id;
			connection = 'active';
			notice = '';
			// Anything already drawn belongs on the first frame.
			changed = !blank;
			armCapture(FAST_INTERVAL_MS);
		} else if (control.type === 'interrupted') {
			// The worker vanished and the API is already picking a replacement;
			// the socket and the session id both survive it.
			connection = 'resuming';
		} else if (control.type === 'resumed') {
			connection = 'active';
			// The browser is the recovery source of truth: the replacement worker
			// has never seen this canvas, so resend all of it.
			changed = true;
			idleTicks = 0;
			armCapture(FAST_INTERVAL_MS);
		} else if (control.type === 'params_updated' && control.params) {
			// Record what the API confirmed. A rejected update leaves these
			// untouched, so the Update button and the slider debounce keep
			// comparing against the last confirmed params rather than optimism.
			const params = control.params as {
				prompt?: unknown;
				structure_strength?: unknown;
				steps?: unknown;
			};
			if (typeof params.prompt === 'string') appliedPrompt = params.prompt;
			if (typeof params.structure_strength === 'number') {
				appliedStructure = params.structure_strength;
			}
			if (typeof params.steps === 'number') appliedSteps = params.steps;
			const wake = afterParamsUpdated();
			changed = wake.changed;
			idleTicks = wake.idleTicks;
			if (sending) armCapture(FAST_INTERVAL_MS);
		} else if (control.type === 'error') {
			// A rejected update reports through the notice and leaves the
			// session alone; the socket stays open either way.
			notice = refusalNotice(control.code ?? 0);
		}
	}

	function refusalNotice(code: number): NoticeKey {
		if (code === 4004) return 'app.realtime_canvas.refused_model';
		if (code === 4003) return 'app.realtime_canvas.refused_capacity';
		if (code === 4002) return 'app.realtime_canvas.refused_version';
		if (code === 4000) return 'app.realtime_canvas.refused_protocol';
		return 'app.realtime_canvas.socket_error';
	}

	/** The picker's option label: the model's measured frame cost when its
	 * worker reported one, so the vega-rt versus sdxl-turbo trade-off is
	 * visible before a session starts. */
	function modelOptionLabel(model: Model): string {
		return model.realtime_p95_ms != null
			? `${model.name} - ${Math.round(model.realtime_p95_ms)} ${t('app.realtime_canvas.latency_ms')}`
			: model.name;
	}

	function onMessage(event: MessageEvent): void {
		if (typeof event.data === 'string') {
			handleControl(event.data);
			return;
		}
		if (!sessionId || !(event.data instanceof ArrayBuffer)) return;
		const image = parseGeneratedFrame(new Uint8Array(event.data), sessionId);
		if (image === null || image.length === 0) return;
		// Latest wins: a frame still waiting to be decoded is dropped rather
		// than queued, so the output cannot fall behind the canvas.
		pendingFrame = image;
		void drainGenerated();
	}

	function connect(): void {
		if (!canConnect) return;
		notice = '';
		userClosing = false;
		connection = 'connecting';
		// A new session must not show the previous one's output.
		sentFrames = 0;
		renderedFrames = 0;
		pendingFrame = null;
		const output = outputCanvas?.getContext('2d');
		if (output) {
			output.fillStyle = PAPER;
			output.fillRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);
		}
		// The open message must carry what the user chose when they pressed
		// Connect, not whatever the reactive state holds by the time the socket
		// completes: an effect can rewrite modelId if the model list changes in
		// between.
		const sessionModelId = modelId;
		const sessionPrompt = prompt;
		const sessionStructure = structureValue;
		const sessionSteps = stepsValue;
		const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
		const opening = new WebSocket(`${scheme}//${location.host}/api/v1/realtime`);
		opening.binaryType = 'arraybuffer';
		socket = opening;
		opening.onopen = () => {
			// openMessage carries the params every shipped realtime model
			// declares: the prompt is required (an open without it is refused
			// 4000 before a worker is assigned), and structure_strength and
			// steps are declared by every shipped realtime model too. The
			// simulated manifest declares only the prompt and accepts the other
			// two as extra properties, which JSON Schema allows. It is built in
			// $lib/realtime-canvas so a test holds it. What opens is what is
			// applied, so the same values seed the applied state that the
			// Update button and the slider debounce compare against.
			appliedPrompt = sessionPrompt.trim();
			appliedStructure = sessionStructure;
			appliedSteps = sessionSteps;
			opening.send(
				openMessage(sessionModelId, sessionPrompt, {
					structure_strength: sessionStructure,
					steps: sessionSteps
				})
			);
		};
		opening.onmessage = onMessage;
		opening.onerror = () => {
			if (!notice) notice = 'app.realtime_canvas.socket_error';
		};
		opening.onclose = (event) => {
			// A late close from a socket that was replaced must not touch live
			// state: its session is gone and the current one is owned by the
			// socket that replaced it.
			if (opening !== socket) return;
			stopTimer();
			socket = null;
			sessionId = null;
			pendingFrame = null;
			connection = userClosing ? 'idle' : stateForCloseCode(event.code);
			if (connection === 'failed' && !notice) notice = refusalNotice(event.code);
		};
	}

	function disconnect(): void {
		userClosing = true;
		stopTimer();
		socket?.close(1000);
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

		<div class="grid min-h-[32rem] flex-1 gap-4 lg:grid-cols-2">
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
					></canvas>
					<div class="flex items-center justify-between gap-2">
						<Button variant="outline" size="sm" onclick={clearCanvas}>
							{t('app.realtime_canvas.clear')}
						</Button>
						<span class="text-muted-foreground text-xs tabular-nums">
							{sentFrames} / {renderedFrames}
						</span>
					</div>
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
