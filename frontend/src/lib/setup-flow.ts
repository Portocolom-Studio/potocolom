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
