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
