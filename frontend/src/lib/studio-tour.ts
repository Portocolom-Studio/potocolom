// The studio tour: short coach marks on the real controls. This module holds the
// step list and the rules for moving between steps, so the rules run without a
// DOM. components/studio-tour.svelte finds the controls and draws the popovers.
import type en from './i18n/en.json';

export type TourStep = {
	/** The data-tour value of the control this step points at. */
	anchor: string;
	titleKey: keyof typeof en;
	bodyKey: keyof typeof en;
};

export const STUDIO_TOUR_STEPS: readonly TourStep[] = [
	{ anchor: 'canvas', titleKey: 'app.tour.canvas_title', bodyKey: 'app.tour.canvas_body' },
	{ anchor: 'prompt', titleKey: 'app.tour.prompt_title', bodyKey: 'app.tour.prompt_body' },
	{ anchor: 'model', titleKey: 'app.tour.model_title', bodyKey: 'app.tour.model_body' },
	{ anchor: 'generate', titleKey: 'app.tour.generate_title', bodyKey: 'app.tour.generate_body' },
	{ anchor: 'images', titleKey: 'app.tour.images_title', bodyKey: 'app.tour.images_body' }
];

/** The index of the next step in `direction` whose control is on screen, or -1
 * when there is none. Pass -1 as `from` to find the first step. A step whose
 * control is missing is passed over, never shown floating. */
export function stepFrom(
	steps: readonly TourStep[],
	from: number,
	direction: 1 | -1,
	isShown: (anchor: string) => boolean
): number {
	for (let index = from + direction; index >= 0 && index < steps.length; index += direction) {
		if (isShown(steps[index].anchor)) return index;
	}
	return -1;
}

/** Where the tour stands after the layout changed: the same step while its
 * control is still on screen, otherwise the next step that is, or -1 when no
 * step is left and the tour should end. */
export function settleStep(
	steps: readonly TourStep[],
	step: number,
	isShown: (anchor: string) => boolean
): number {
	if (isShown(steps[step].anchor)) return step;
	return stepFrom(steps, step, 1, isShown);
}

/** Set when the tour is finished or skipped. Its absence is what auto-starts it. */
export const TOUR_DONE_KEY = 'potocolom-tour-done';
// Written and removed to prove storage takes writes. Never read back.
const TOUR_PROBE_KEY = 'potocolom-tour-probe';

type TourStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

// Storage is passed as a getter: blocked storage throws on the access itself,
// so the read has to sit inside the try.

/** Auto-start only when storage can be read and written and the flag is unset.
 * Blocked storage never auto-starts. Readable storage that refuses writes
 * does not either: finishing could not save the flag, so the tour would come
 * back on every load. */
export function shouldAutoStart(storage: () => TourStorage): boolean {
	try {
		const store = storage();
		if (store.getItem(TOUR_DONE_KEY) !== null) return false;
		store.setItem(TOUR_PROBE_KEY, '1');
		store.removeItem(TOUR_PROBE_KEY);
		return true;
	} catch {
		return false;
	}
}

export function markTourDone(storage: () => TourStorage): void {
	try {
		storage().setItem(TOUR_DONE_KEY, '1');
	} catch {
		// Blocked storage: no auto-start happens anyway, and Show tour still works.
	}
}
