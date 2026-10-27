// Swipe to dismiss for edge panels on touch screens: the mobile sidebar
// sheet and the Images inspector overlay. The panel follows the finger,
// closes when it travelled far enough or was flicked, and springs back
// otherwise. The math is pure and unit tested; attachSwipeDismiss wires it
// to pointer events.

/** +1 dismisses toward the right (an end-side panel), -1 toward the left. */
export type SwipeDirection = 1 | -1;

export type SwipeRelease = {
	/** Signed offset of the panel along the axis, in px. */
	offset: number;
	/** Panel size along the axis, in px. */
	size: number;
	/** Signed release velocity, in px/ms. */
	velocity: number;
	direction: SwipeDirection;
};

/** Fraction of its own width a panel must travel to close on release. */
export const DISMISS_DISTANCE_RATIO = 0.35;
/** A flick this fast toward the edge closes it whatever the distance. */
export const DISMISS_VELOCITY = 0.45;
/** Movement before the gesture decides between swiping and scrolling. */
export const LOCK_DISTANCE = 8;
/** How much of a drag against the dismiss direction the panel shows. */
const RESISTANCE = 0.15;

/** Where the panel sits for a finger that moved `delta` px: one to one
 * toward the edge, damped the other way so the panel cannot be pulled in. */
export function swipeOffset(delta: number, direction: SwipeDirection): number {
	const toward = delta * direction;
	return toward >= 0 ? delta : delta * RESISTANCE;
}

/** Whether a released swipe closes the panel. */
export function shouldDismiss(release: SwipeRelease): boolean {
	const travelled = release.offset * release.direction;
	if (travelled <= 0) return false;
	if (release.size > 0 && travelled >= release.size * DISMISS_DISTANCE_RATIO) return true;
	// A flick needs a little travel too, so a tap that jitters cannot close.
	return release.velocity * release.direction >= DISMISS_VELOCITY && travelled >= LOCK_DISTANCE;
}

/** Decide, once the finger moved far enough, whether the gesture is a swipe
 * along the dismiss direction or something else (a scroll) left alone.
 * Null while it is too early to tell. */
export function lockGesture(
	dx: number,
	dy: number,
	direction: SwipeDirection
): 'swipe' | 'other' | null {
	if (Math.hypot(dx, dy) < LOCK_DISTANCE) return null;
	return Math.abs(dx) > Math.abs(dy) * 1.2 && dx * direction > 0 ? 'swipe' : 'other';
}

/** Velocity from the last pointer samples, smoothed so one noisy sample
 * does not decide a flick. Samples older than `windowMs` are ignored. */
export function releaseVelocity(
	samples: { x: number; t: number }[],
	now: number,
	windowMs = 100
): number {
	const recent = samples.filter((sample) => now - sample.t <= windowMs);
	if (recent.length < 2) return 0;
	const first = recent[0];
	const last = recent[recent.length - 1];
	const elapsed = last.t - first.t;
	return elapsed > 0 ? (last.x - first.x) / elapsed : 0;
}

export type SwipeDismissOptions = {
	direction: SwipeDirection;
	/** Checked at the start of each gesture: off on wide screens. */
	enabled?: () => boolean;
	/** Close the panel. The inline offset is removed first, so the panel's
	 * own closing transition runs from where the finger left it. */
	onDismiss: () => void;
};

const SPRING_BACK = 'translate 360ms cubic-bezier(0.34, 1.4, 0.64, 1)';

function prefersReducedMotion(): boolean {
	return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
}

/** Follow touch and pen drags on `panel` and dismiss it past the threshold.
 * The panel needs touch-action: pan-y so the browser leaves horizontal
 * moves to the page while vertical scrolling stays native. */
export function attachSwipeDismiss(panel: HTMLElement, options: SwipeDismissOptions): () => void {
	let pointerId: number | null = null;
	let startX = 0;
	let startY = 0;
	let lock: 'swipe' | 'other' | null = null;
	let offset = 0;
	let samples: { x: number; t: number }[] = [];
	let swallowClick = false;

	const reset = () => {
		pointerId = null;
		lock = null;
		offset = 0;
		samples = [];
	};

	const onPointerDown = (event: PointerEvent) => {
		if (event.pointerType === 'mouse' || pointerId !== null) return;
		if (options.enabled && !options.enabled()) return;
		pointerId = event.pointerId;
		startX = event.clientX;
		startY = event.clientY;
		lock = null;
		samples = [{ x: event.clientX, t: event.timeStamp }];
	};

	const onPointerMove = (event: PointerEvent) => {
		if (event.pointerId !== pointerId) return;
		const dx = event.clientX - startX;
		const dy = event.clientY - startY;
		if (lock === null) {
			lock = lockGesture(dx, dy, options.direction);
			if (lock === 'swipe') {
				panel.setPointerCapture(event.pointerId);
				panel.style.transition = 'none';
			}
		}
		if (lock !== 'swipe') return;
		event.preventDefault();
		offset = swipeOffset(dx, options.direction);
		panel.style.translate = `${offset}px 0`;
		samples = [...samples.slice(-5), { x: event.clientX, t: event.timeStamp }];
	};

	const finish = (event: PointerEvent, cancelled: boolean) => {
		if (event.pointerId !== pointerId) return;
		if (lock === 'swipe') {
			swallowClick = true;
			if (panel.hasPointerCapture(event.pointerId)) panel.releasePointerCapture(event.pointerId);
			const dismiss =
				!cancelled &&
				shouldDismiss({
					offset,
					size: panel.getBoundingClientRect().width,
					velocity: releaseVelocity(samples, event.timeStamp),
					direction: options.direction
				});
			if (dismiss) {
				panel.style.transition = '';
				panel.style.translate = '';
				options.onDismiss();
			} else if (prefersReducedMotion()) {
				// No spring with reduced motion: the panel goes straight back.
				// The style is flushed before its own transition returns, so
				// that transition does not animate the jump either.
				panel.style.transition = 'none';
				panel.style.translate = '';
				void panel.offsetWidth;
				panel.style.transition = '';
			} else {
				panel.style.transition = SPRING_BACK;
				panel.style.translate = '';
				const settle = () => {
					panel.style.transition = '';
					panel.removeEventListener('transitionend', settle);
				};
				panel.addEventListener('transitionend', settle);
				setTimeout(settle, 400);
			}
		}
		reset();
	};

	const onPointerUp = (event: PointerEvent) => finish(event, false);
	const onPointerCancel = (event: PointerEvent) => finish(event, true);
	// The release of a swipe is not a click on whatever sat under the finger.
	const onClickCapture = (event: MouseEvent) => {
		if (!swallowClick) return;
		swallowClick = false;
		event.preventDefault();
		event.stopPropagation();
	};
	const onPointerDownCapture = () => {
		swallowClick = false;
	};

	panel.addEventListener('pointerdown', onPointerDownCapture, true);
	panel.addEventListener('pointerdown', onPointerDown);
	panel.addEventListener('pointermove', onPointerMove);
	panel.addEventListener('pointerup', onPointerUp);
	panel.addEventListener('pointercancel', onPointerCancel);
	panel.addEventListener('click', onClickCapture, true);
	return () => {
		panel.removeEventListener('pointerdown', onPointerDownCapture, true);
		panel.removeEventListener('pointerdown', onPointerDown);
		panel.removeEventListener('pointermove', onPointerMove);
		panel.removeEventListener('pointerup', onPointerUp);
		panel.removeEventListener('pointercancel', onPointerCancel);
		panel.removeEventListener('click', onClickCapture, true);
		panel.style.translate = '';
		panel.style.transition = '';
	};
}
