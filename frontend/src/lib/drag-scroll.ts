const DRAG_THRESHOLD_PX = 12;
const INERTIA_MIN_PX_PER_MS = 0.04;
const INERTIA_FRICTION_PER_MS = 0.0028;
const INERTIA_VELOCITY_BOOST = 1.15;
const VELOCITY_EMA_ALPHA = 0.35;
const SCROLL_KEYS = new Set(['ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', 'Home', 'End', ' ']);

type DragState = {
	pointerId: number;
	startY: number;
	startScrollTop: number;
	lastY: number;
	lastTime: number;
	velocity: number;
	moved: boolean;
};

export function dragScroll(scroller: HTMLElement, onUserScroll: () => void): () => void {
	let drag: DragState | null = null;
	let animationFrame = 0;
	let suppressClick = false;
	let suppressedPointerId: number | null = null;
	let clearSuppressionTimer: ReturnType<typeof setTimeout> | undefined;

	function clearSuppression(): void {
		if (clearSuppressionTimer !== undefined) clearTimeout(clearSuppressionTimer);
		clearSuppressionTimer = undefined;
		suppressClick = false;
	}

	function stopCoast(): void {
		if (animationFrame !== 0) cancelAnimationFrame(animationFrame);
		animationFrame = 0;
	}

	function stopDrag(): void {
		const active = drag;
		drag = null;
		scroller.classList.remove('is-drag-scrolling');
		if (active && scroller.hasPointerCapture(active.pointerId)) {
			scroller.releasePointerCapture(active.pointerId);
		}
	}

	function startCoast(velocity: number): void {
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
		let speed = velocity * INERTIA_VELOCITY_BOOST;
		if (Math.abs(speed) < INERTIA_MIN_PX_PER_MS) return;
		let previous = performance.now();
		const step = (now: number) => {
			const deltaTime = Math.min(32, now - previous);
			previous = now;
			speed *= Math.exp(-INERTIA_FRICTION_PER_MS * deltaTime);
			if (Math.abs(speed) < INERTIA_MIN_PX_PER_MS) {
				animationFrame = 0;
				return;
			}
			const max = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
			scroller.scrollTop = Math.max(0, Math.min(max, scroller.scrollTop + speed * deltaTime));
			if (scroller.scrollTop <= 0 || scroller.scrollTop >= max) {
				animationFrame = 0;
				return;
			}
			animationFrame = requestAnimationFrame(step);
		};
		animationFrame = requestAnimationFrame(step);
	}

	function onPointerDown(event: PointerEvent): void {
		onUserScroll();
		const wasCoasting = animationFrame !== 0;
		stopCoast();
		clearSuppression();
		suppressedPointerId = null;
		if (
			event.pointerType !== 'mouse' ||
			event.button !== 0 ||
			event.ctrlKey ||
			event.metaKey ||
			event.shiftKey ||
			event.altKey
		)
			return;
		const target = event.target;
		if (
			target instanceof Element &&
			target.closest(
				'input, textarea, select, a[href], [contenteditable], [data-drag-scroll-ignore]'
			)
		)
			return;
		const rect = scroller.getBoundingClientRect();
		if (event.clientX >= rect.left + scroller.clientLeft + scroller.clientWidth) return;
		if (wasCoasting) {
			suppressClick = true;
			suppressedPointerId = event.pointerId;
			return;
		}
		drag = {
			pointerId: event.pointerId,
			startY: event.clientY,
			startScrollTop: scroller.scrollTop,
			lastY: event.clientY,
			lastTime: event.timeStamp,
			velocity: 0,
			moved: false
		};
	}

	function onPointerMove(event: PointerEvent): void {
		if (drag === null || event.pointerId !== drag.pointerId) return;
		const delta = event.clientY - drag.startY;
		if (!drag.moved) {
			if (Math.abs(delta) < DRAG_THRESHOLD_PX) return;
			drag.moved = true;
			scroller.setPointerCapture(event.pointerId);
			scroller.classList.add('is-drag-scrolling');
			onUserScroll();
		}
		const sampleTime = event.timeStamp - drag.lastTime;
		if (sampleTime > 0) {
			const sample = (drag.lastY - event.clientY) / sampleTime;
			drag.velocity =
				drag.velocity === 0
					? sample
					: VELOCITY_EMA_ALPHA * sample + (1 - VELOCITY_EMA_ALPHA) * drag.velocity;
		}
		drag.lastY = event.clientY;
		drag.lastTime = event.timeStamp;
		const max = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
		scroller.scrollTop = Math.max(0, Math.min(max, drag.startScrollTop - delta));
	}

	function onPointerUp(event: PointerEvent): void {
		if (drag === null) {
			if (suppressedPointerId !== event.pointerId) return;
			suppressedPointerId = null;
			clearSuppressionTimer = setTimeout(clearSuppression, 0);
			return;
		}
		if (event.pointerId !== drag.pointerId) return;
		const active = drag;
		const releaseVelocity = event.timeStamp - active.lastTime > 80 ? 0 : active.velocity;
		stopDrag();
		if (!active.moved) return;
		suppressClick = true;
		startCoast(releaseVelocity);
		clearSuppressionTimer = setTimeout(clearSuppression, 0);
	}

	function onCancel(): void {
		stopCoast();
		stopDrag();
		suppressedPointerId = null;
		clearSuppression();
	}

	function onLostCapture(event: PointerEvent): void {
		if (drag?.pointerId === event.pointerId) onCancel();
	}

	function onClick(event: MouseEvent): void {
		if (!suppressClick) return;
		event.preventDefault();
		event.stopImmediatePropagation();
		clearSuppression();
	}

	function onWheel(event: WheelEvent): void {
		stopCoast();
		if (event.deltaX !== 0 || event.deltaY !== 0) onUserScroll();
	}

	function onKeyDown(event: KeyboardEvent): void {
		stopCoast();
		if (SCROLL_KEYS.has(event.key)) onUserScroll();
	}

	const stop = () => {
		stopCoast();
		stopDrag();
		suppressedPointerId = null;
		clearSuppression();
	};
	scroller.addEventListener('pointerdown', onPointerDown);
	scroller.addEventListener('pointermove', onPointerMove);
	scroller.addEventListener('pointerup', onPointerUp);
	scroller.addEventListener('pointercancel', onCancel);
	scroller.addEventListener('lostpointercapture', onLostCapture);
	scroller.addEventListener('click', onClick, true);
	scroller.addEventListener('wheel', onWheel, { passive: true });
	scroller.addEventListener('keydown', onKeyDown);
	window.addEventListener('pointerup', onPointerUp);
	window.addEventListener('pointercancel', onCancel);
	window.addEventListener('blur', onCancel);

	return () => {
		stop();
		scroller.removeEventListener('pointerdown', onPointerDown);
		scroller.removeEventListener('pointermove', onPointerMove);
		scroller.removeEventListener('pointerup', onPointerUp);
		scroller.removeEventListener('pointercancel', onCancel);
		scroller.removeEventListener('lostpointercapture', onLostCapture);
		scroller.removeEventListener('click', onClick, true);
		scroller.removeEventListener('wheel', onWheel);
		scroller.removeEventListener('keydown', onKeyDown);
		window.removeEventListener('pointerup', onPointerUp);
		window.removeEventListener('pointercancel', onCancel);
		window.removeEventListener('blur', onCancel);
	};
}
