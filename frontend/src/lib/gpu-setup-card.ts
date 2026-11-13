// The one-time "Where are your GPUs?" card. Its rules run without a DOM; the
// card itself is components/studio-gpu-card.svelte.

/** Set when the card is finished or dismissed. Its absence is what shows it. */
export const GPU_CARD_DONE_KEY = 'potocolom-gpu-card-done';
// Written and removed to prove storage takes writes. Never read back.
const GPU_CARD_PROBE_KEY = 'potocolom-gpu-card-probe';

type CardStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export type GpuOption = 'this' | 'other';

// Storage is passed as a getter: blocked storage throws on the access itself,
// so the read has to sit inside the try. The same rules as the tour's flag,
// because a card that cannot save its flag would come back on every load.
export function shouldShowGpuCard(canAdminister: boolean, storage: () => CardStorage): boolean {
	if (!canAdminister) return false;
	try {
		const store = storage();
		if (store.getItem(GPU_CARD_DONE_KEY) !== null) return false;
		store.setItem(GPU_CARD_PROBE_KEY, '1');
		store.removeItem(GPU_CARD_PROBE_KEY);
		return true;
	} catch {
		return false;
	}
}

export function markGpuCardDone(storage: () => CardStorage): void {
	try {
		storage().setItem(GPU_CARD_DONE_KEY, '1');
	} catch {
		// Blocked storage: the card cannot show again on this browser anyway.
	}
}

// A null list (still loading, or the request failed) keeps the "this" option:
// the failure must not send the user to a dialog that cannot show their GPUs.
export function defaultGpuOption(workers: string[] | null): GpuOption {
	return workers !== null && workers.length === 0 ? 'other' : 'this';
}

// Only "Other computers" leaves the studio for the fleet tab. The tour starts
// after the card closes by its own first-visit rules, so it never starts on top
// of the Administration view.
export function gpuCardOpensFleet(ending: GpuOption | 'dismissed'): boolean {
	return ending === 'other';
}
