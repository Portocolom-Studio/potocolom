// Back closes transient studio UI before it leaves the view: opening the
// Images inspector, the command palette or the mobile sidebar pushes a
// shallow history entry (SvelteKit's pushState, so no navigation runs), and
// Back pops it, which closes the overlay. Closing one any other way pops its
// entry too, so the history does not fill with dead steps.
import { pushState, replaceState } from '$app/navigation';
import { page } from '$app/state';

export type StudioOverlay = 'inspector' | 'command' | 'sidebar';

const STATES_KEY = 'sveltekit:states';

function overlayOf(state: unknown): StudioOverlay | undefined {
	if (typeof state !== 'object' || state === null) return undefined;
	const states = (state as Record<string, unknown>)[STATES_KEY];
	if (typeof states !== 'object' || states === null || Array.isArray(states)) return undefined;
	const overlay = (states as Record<string, unknown>).studioOverlay;
	return overlay === 'inspector' || overlay === 'command' || overlay === 'sidebar'
		? overlay
		: undefined;
}

/** Give an overlay that just opened its own history entry. */
export function pushOverlay(kind: StudioOverlay): void {
	if (page.state.studioOverlay === kind) return;
	try {
		const nextState = { ...page.state, studioOverlay: kind };
		if (overlayOf(history.state) === kind) replaceState('', nextState);
		else pushState('', nextState);
	} catch {
		// Before the router starts there is no history to add to.
	}
}

/** Drop the entry of an overlay closed by a button, Escape or a choice.
 * Resolves once the history has stepped back, so a navigation that follows
 * lands after it rather than being undone by it. */
export function popOverlay(kind: StudioOverlay): Promise<void> {
	if (page.state.studioOverlay !== kind) return Promise.resolve();
	return new Promise((resolve) => {
		const done = () => {
			window.removeEventListener('popstate', done);
			clearTimeout(timer);
			resolve();
		};
		const timer = setTimeout(done, 400);
		window.addEventListener('popstate', done);
		history.back();
	});
}

/** An overlay's entry is still in the history after the overlay itself is
 * gone (a view left and returned to): forget it without moving. */
export function forgetOverlay(kind: StudioOverlay): void {
	if (page.state.studioOverlay !== kind) return;
	try {
		replaceState('', { ...page.state, studioOverlay: undefined });
	} catch {
		// As in pushOverlay.
	}
}

/** Close the overlay when Back (or Forward) moves to an entry that is not
 * its own. Returns the listener's cleanup, for an $effect. */
export function closeOnHistoryStep(
	kind: StudioOverlay,
	isOpen: () => boolean,
	close: () => void
): () => void {
	const onPopState = (event: PopStateEvent) => {
		if (isOpen() && overlayOf(event.state) !== kind) close();
	};
	window.addEventListener('popstate', onPopState);
	return () => window.removeEventListener('popstate', onPopState);
}
