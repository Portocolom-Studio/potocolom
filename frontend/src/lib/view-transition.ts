// Studio view changes and the canvas's Workbench and Focus switch crossfade
// through the View Transitions API where the browser has it. Elsewhere, and
// under prefers-reduced-motion, the change simply happens.
import { flushSync } from 'svelte';

export function viewTransitionsEnabled(): boolean {
	return (
		typeof document !== 'undefined' &&
		typeof document.startViewTransition === 'function' &&
		!window.matchMedia('(prefers-reduced-motion: reduce)').matches
	);
}

// While a transition runs the page takes no input: every hit lands on the
// document element. The crossfade lasts 180ms once the new view is ready; a
// transition still running well after that (a compositor that never
// animates it, as in headless Chrome without a GPU), or whose update never
// finishes, is skipped, so it can never hold the studio frozen.
const ANIMATION_LIMIT_MS = 300;
const UPDATE_LIMIT_MS = 1000;

/** Start a view transition with those limits applied. */
export function startStudioViewTransition(update: () => void | Promise<void>): void {
	const transition = document.startViewTransition(update);
	let limit = setTimeout(() => transition.skipTransition(), UPDATE_LIMIT_MS);
	transition.ready.then(
		() => {
			clearTimeout(limit);
			limit = setTimeout(() => transition.skipTransition(), ANIMATION_LIMIT_MS);
		},
		() => clearTimeout(limit)
	);
	transition.finished.then(
		() => clearTimeout(limit),
		() => clearTimeout(limit)
	);
}

/** Run a state change inside a view transition. flushSync applies it to the
 * DOM before the browser takes the new snapshot. */
export function withViewTransition(update: () => void): void {
	if (!viewTransitionsEnabled()) {
		update();
		return;
	}
	startStudioViewTransition(() => flushSync(update));
}
