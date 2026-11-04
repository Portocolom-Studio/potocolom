export const GIB = 1024 ** 3;

export function formatSize(bytes: number, locale: string): string {
	return `${new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(bytes / GIB)} GiB`;
}

export function knownSize(value: number | null | undefined): number | null {
	return value != null && value > 0 ? value : null;
}
export function remainingReservation(
	total: number | null,
	fallback: number | null,
	done: number
): number | null {
	const size = knownSize(total) ?? knownSize(fallback);
	return size === null ? null : Math.max(0, size - done);
}
export function sumKnown(values: (number | null)[]): number | null {
	return values.some((value) => value === null)
		? null
		: values.reduce<number>((sum, value) => sum + (value ?? 0), 0);
}
export function selectionSpace(
	free: number | null,
	sizes: (number | null)[],
	reserved: number | null = 0
) {
	const total = sumKnown(sizes.map(knownSize));
	const freeAfter =
		free === null || reserved === null || total === null ? null : free - reserved - total;
	return {
		total,
		freeAfter,
		allowed: sizes.length > 0 && freeAfter !== null && freeAfter >= 5 * GIB
	};
}

export function diskPreview(total: number, free: number, reserved: number, selected: number) {
	return {
		used: Math.max(0, Math.min(total, total - free)),
		reserved: Math.min(Math.max(0, free), reserved),
		selected: Math.min(Math.max(0, free - reserved), selected),
		freeAfter: free - reserved - selected
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
