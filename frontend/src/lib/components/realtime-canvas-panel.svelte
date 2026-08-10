<script lang="ts">
	// The live realtime drawing surface (issue #3). One 512 by 512 bitmap, CSS
	// scaled for display, sent as complete WebP frames over the realtime
	// protocol. The framing and the send policy live in $lib/realtime-canvas so
	// they stay testable; this file owns only the DOM and the socket.
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
	import { studio } from '$lib/studio.svelte';
	import {
		FAST_INTERVAL_MS,
		IDLE_TICKS_BEFORE_STOP,
		canvasFrame,
		nextIntervalMs,
		parseGeneratedFrame,
		shouldSendFrame,
		stateForCloseCode,
		type ConnectionState
	} from '$lib/realtime-canvas';

	/** The wire dimensions. CSS scales the display without changing these. */
	const CANVAS_SIZE = 512;
	const STROKE_WIDTH = 6;

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

	// Only a model advertising the realtime capability can take canvas frames.
	const realtimeModels = $derived(
		studio.models.filter((model) => model.capabilities.includes('realtime'))
	);
	const modelId = $derived(realtimeModels[0]?.id ?? '');
	const connected = $derived(connection === 'active' || connection === 'resuming');
	const busy = $derived(connection === 'connecting' || connected);
	const canConnect = $derived(!busy && modelId !== '' && prompt.trim() !== '');

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
		if (!drawCanvas) return;
		paint = drawCanvas.getContext('2d');
		if (!paint) return;
		// Opaque white: a transparent canvas would reach the model as
		// transparency rather than as the blank paper the user sees.
		paint.fillStyle = '#ffffff';
		paint.fillRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);
		paint.lineWidth = STROKE_WIDTH;
		paint.lineCap = 'round';
		paint.lineJoin = 'round';
		paint.strokeStyle = '#111827';
	});

	// Tear the socket and the timer down with the panel, so leaving the view
	// does not leave a session open on a worker.
	$effect(() => () => {
		userClosing = true;
		stopTimer();
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
		if (connected && timer === null) armCapture(FAST_INTERVAL_MS);
	}

	/** One segment per move, so a long stroke does not restroke its whole path. */
	function drawSegment(from: { x: number; y: number }, to: { x: number; y: number }): void {
		if (!paint) return;
		paint.beginPath();
		paint.moveTo(from.x, from.y);
		paint.lineTo(to.x, to.y);
		paint.stroke();
	}

	function onPointerDown(event: PointerEvent): void {
		if (!event.isPrimary || !paint) return;
		drawing = true;
		strokePointer = event.pointerId;
		(event.currentTarget as HTMLCanvasElement).setPointerCapture(event.pointerId);
		const point = canvasPoint(event);
		// A tap that never moves should still leave a mark; the round cap makes
		// a zero length segment a dot.
		drawSegment(point, point);
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
		paint.fillStyle = '#ffffff';
		paint.fillRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);
		blank = true;
		// Still a revision worth sending: the model should stop drawing what is
		// no longer on the canvas.
		changed = true;
		idleTicks = 0;
		if (connected && timer === null) armCapture(FAST_INTERVAL_MS);
	}

	function stopTimer(): void {
		if (timer !== null) clearTimeout(timer);
		timer = null;
	}

	function armCapture(delay: number): void {
		stopTimer();
		timer = setTimeout(() => void captureTick(), delay);
	}

	function encodeCanvas(canvas: HTMLCanvasElement): Promise<Uint8Array> {
		return new Promise((resolve, reject) => {
			// WebP per docs/connection-handling.md. A browser that cannot encode
			// WebP silently returns PNG, which the worker's decoder also opens.
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

		if (!shouldSendFrame({ changed, encoding, buffered: socket.bufferedAmount })) {
			idleTicks += 1;
			// Nothing to send for a while: stop arming rather than hold a timer
			// open on an untouched canvas. Paint, a clear, or a resume restarts it.
			if (idleTicks >= IDLE_TICKS_BEFORE_STOP && !changed) return;
			armCapture(nextIntervalMs(lastFrameCostMs));
			return;
		}

		const started = performance.now();
		// Cleared before the encode, so paint arriving during it marks the canvas
		// changed again rather than being swallowed by this frame.
		changed = false;
		encoding = true;
		try {
			const image = await encodeCanvas(drawCanvas);
			if (socket && socket.readyState === WebSocket.OPEN && sessionId) {
				socket.send(canvasFrame(sessionId, image));
				sentFrames += 1;
			}
		} catch {
			// A failed encode must not end the session silently: keep the
			// revision pending and let the next tick try again.
			changed = true;
			notice = 'app.realtime_canvas.encode_failed';
		} finally {
			encoding = false;
			lastFrameCostMs = performance.now() - started;
		}
		armCapture(nextIntervalMs(lastFrameCostMs));
	}

	async function drainGenerated(): Promise<void> {
		if (decoding) return;
		decoding = true;
		try {
			while (pendingFrame !== null) {
				const image = pendingFrame;
				pendingFrame = null;
				// The Blob copies the bytes, so the socket's buffer is free to go.
				const bitmap = await createImageBitmap(new Blob([image], { type: 'image/webp' }));
				const target = outputCanvas?.getContext('2d');
				if (target) {
					target.drawImage(bitmap, 0, 0, CANVAS_SIZE, CANVAS_SIZE);
					renderedFrames += 1;
				}
				bitmap.close();
			}
		} catch {
			notice = 'app.realtime_canvas.decode_failed';
		} finally {
			decoding = false;
		}
	}

	function handleControl(text: string): void {
		let control: { type?: string; session_id?: string; code?: number };
		try {
			control = JSON.parse(text) as typeof control;
		} catch {
			return;
		}
		if (control.type === 'ready' && control.session_id) {
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
		} else if (control.type === 'error') {
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
		const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
		const opening = new WebSocket(`${scheme}//${location.host}/api/v1/realtime`);
		opening.binaryType = 'arraybuffer';
		socket = opening;
		opening.onopen = () => {
			// params must carry the prompt: every realtime manifest marks it
			// required, and an open without it is refused with 4000 before a
			// worker is ever assigned (backend/app/realtime.py).
			opening.send(
				JSON.stringify({
					type: 'open',
					model_id: modelId,
					params: { prompt: prompt.trim() }
				})
			);
		};
		opening.onmessage = onMessage;
		opening.onerror = () => {
			if (!notice) notice = 'app.realtime_canvas.socket_error';
		};
		opening.onclose = (event) => {
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
				<Card.Content class="flex min-h-0 flex-1 flex-col gap-3">
					<canvas
						bind:this={drawCanvas}
						width={CANVAS_SIZE}
						height={CANVAS_SIZE}
						aria-label={t('app.realtime_canvas.draw_surface')}
						class="border-border w-full max-w-full touch-none rounded-lg border bg-white"
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
				<Card.Content class="flex min-h-0 flex-1 flex-col gap-3">
					<div class="relative">
						<canvas
							bind:this={outputCanvas}
							width={CANVAS_SIZE}
							height={CANVAS_SIZE}
							class="border-border bg-muted/20 w-full max-w-full rounded-lg border"
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
						<Label for="realtime-prompt">{t('app.gen.prompt')}</Label>
						<Input
							id="realtime-prompt"
							bind:value={prompt}
							disabled={busy}
							placeholder={t('app.realtime_canvas.prompt_placeholder')}
						/>
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
						<p class="text-destructive text-sm">{t(notice)}</p>
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
