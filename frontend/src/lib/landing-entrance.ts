// The landing entrance gate. The reveal decision is pure so the timeout that
// bounds a hung asset preload is testable without a browser.
export const LANDING_ENTRANCE_TIMEOUT_MS = 8000;

// The gate may move to the reveal either once the assets are loaded and the
// minimum spin has elapsed, or as soon as the absolute deadline passes, so a
// single hung image can never leave header and main inert forever.
export function shouldRevealLandingEntrance(timing: {
	startedAtMs: number;
	nowMs: number;
	assetsLoaded: boolean;
	minimumSpinMs: number;
	timeoutMs: number;
}): boolean {
	const elapsed = timing.nowMs - timing.startedAtMs;
	if (elapsed >= timing.timeoutMs) return true;
	return timing.assetsLoaded && elapsed >= timing.minimumSpinMs;
}

// The reveal pause between the 'revealing' and 'ready' phases. Normally the
// full revealMs runs; when the absolute deadline is near, the pause is cut
// short so the ready state still lands on the deadline.
export function landingRevealPauseMs(timing: {
	startedAtMs: number;
	nowMs: number;
	revealMs: number;
	timeoutMs: number;
}): number {
	const remaining = timing.startedAtMs + timing.timeoutMs - timing.nowMs;
	if (remaining <= 0) return 0;
	return Math.min(timing.revealMs, remaining);
}
