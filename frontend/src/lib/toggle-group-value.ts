// bits-ui writes '' into a single toggle group when its active item is
// clicked again, which renders every item off while the caller still holds the
// old value (issue #500). A single group keeps its last choice instead.
export function acceptsToggleValue(type: string | undefined, next: unknown): boolean {
	return !(type === 'single' && next === '');
}
