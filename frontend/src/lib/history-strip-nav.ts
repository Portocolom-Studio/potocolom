export type StripNavKey = 'ArrowLeft' | 'ArrowRight' | 'Home' | 'End';

export function isStripNavKey(key: string): key is StripNavKey {
	return key === 'ArrowLeft' || key === 'ArrowRight' || key === 'Home' || key === 'End';
}

// Roving focus movement for the history strip. The index clamps at the ends:
// ArrowLeft on the first thumbnail stays on it, ArrowRight on the last stays
// on it, Home and End jump to the first and last.
export function nextFocusIndex(current: number, count: number, key: StripNavKey): number {
	if (count <= 0) return current;
	switch (key) {
		case 'Home':
			return 0;
		case 'End':
			return count - 1;
		case 'ArrowLeft':
			return Math.max(0, current - 1);
		case 'ArrowRight':
			return Math.min(count - 1, current + 1);
	}
}

// A thumbnail's accessible name: the prompt when it carries one, else the
// caller's localized untitled label. Whitespace alone is not a prompt.
export function thumbnailLabel(prompt: string | null | undefined, untitled: string): string {
	return prompt !== null && prompt !== undefined && prompt.trim() !== '' ? prompt : untitled;
}
