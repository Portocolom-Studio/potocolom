export type LandingMode = 'dark' | 'light';

const KEY = 'landing-mode';

// Storage throws in private windows and when site data is blocked. The theme then
// applies for this visit only, which is the right fallback for a preference.
export function readLandingMode(): LandingMode {
	try {
		return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark';
	} catch {
		return 'dark';
	}
}

export function applyLandingMode(mode: LandingMode, remember = true): void {
	document.documentElement.dataset.landingMode = mode;
	if (!remember) return;
	try {
		localStorage.setItem(KEY, mode);
	} catch {
		return;
	}
}
