export const GIB = 1024 ** 3;

export function formatSize(bytes: number): string {
	return `${(bytes / GIB).toFixed(2)} GiB`;
}

export function selectionSpace(free: number, sizes: (number | null)[], reserved = 0) {
	const total = sizes.reduce<number>((sum, size) => sum + (size ?? 0), 0);
	const freeAfter = free - reserved - total;
	return {
		total,
		freeAfter,
		allowed: sizes.length > 0 && sizes.every((size) => size !== null) && freeAfter >= 5 * GIB
	};
}

export function shouldPoll(states: string[]): boolean {
	return states.some((state) => state === 'queued' || state === 'downloading');
}

export function pollInterval(mounted: boolean, active: boolean): number | null {
	return mounted ? (active ? 1000 : 10000) : null;
}

export function initialSelection(
	models: {
		id: string;
		suggested: boolean;
		installed: boolean;
		needs_token: boolean;
		fits: boolean;
	}[]
): string[] {
	if (models.some((model) => model.installed)) return [];
	return models
		.filter((model) => model.suggested && model.fits && !model.needs_token)
		.map((model) => model.id);
}
