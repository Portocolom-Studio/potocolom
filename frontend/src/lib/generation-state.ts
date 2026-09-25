export function isCancellable(state: string): boolean {
	return state === 'queued' || state === 'running';
}
