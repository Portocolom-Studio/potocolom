// The setup link's token rules and the unclaimed-install redirect. Kept pure
// so the rules can be tested without a browser.

// The server mints 43 URL-safe characters. The bounds are loose on purpose,
// so a token of another length still parses and only junk is refused.
const TOKEN_SHAPE = /^[A-Za-z0-9_-]{16,256}$/;

export function setupTokenFromHash(hash: string): string | null {
	const trimmed = hash.trim();
	const candidate = trimmed.startsWith('#') ? trimmed.slice(1) : trimmed;
	return TOKEN_SHAPE.test(candidate) ? candidate : null;
}

// Only an explicit true sends the browser to setup. An unreadable or older
// config keeps the studio where it was.
export function setupRequiredFrom(config: unknown): boolean {
	if (typeof config !== 'object' || config === null) return false;
	return (config as { setup_required?: unknown }).setup_required === true;
}

export type SetupStep = 'choose' | 'claim' | 'decided';

// What the setup page offers a link. An unclaimed install gets the choice. An
// install that an AUTH_MODE override decided as accounts gets the admin claim
// alone, because its mode is already fixed. Anything else is already set up.
export function setupStepFrom(config: unknown): SetupStep {
	if (setupRequiredFrom(config)) return 'choose';
	if (typeof config !== 'object' || config === null) return 'decided';
	const methods = (config as { auth_methods?: unknown }).auth_methods;
	return Array.isArray(methods) && methods.length > 0 ? 'claim' : 'decided';
}

// The whole seconds the status answer gives the link to live. Anything but a
// whole, non-negative count is treated as absent, so an older API shows no timer.
export function setupExpiryFrom(body: unknown): number | null {
	if (typeof body !== 'object' || body === null) return null;
	const seconds = (body as { expires_in?: unknown }).expires_in;
	return typeof seconds === 'number' && Number.isInteger(seconds) && seconds >= 0 ? seconds : null;
}

// Rounded up, so the card never shows 0 min while the link still works.
export function minutesLeft(seconds: number): number {
	return Math.ceil(seconds / 60);
}
