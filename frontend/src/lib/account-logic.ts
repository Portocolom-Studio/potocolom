import { resolve } from '$app/paths';

// Pure helpers for the studio account view: the values its dialogs and forms
// gate on. Kept out of the component so they have tests.

// The typed confirmation must match the account's own address, ignoring the
// whitespace a paste brings along and the capitalisation people type.
export function deleteConfirmed(typed: string, email: string): boolean {
	return typed.trim().toLowerCase() === email.trim().toLowerCase();
}

// What this install offers and what the account already has, minus the two
// answers that are not linking: a password is not a provider to send
// somebody to, and a provider already on the account has nothing to add.
// The install's own order is kept, because that is the order the sign-in
// page offers the same providers in.
export function linkableProviders(authMethods: string[], identities: string[]): string[] {
	const linked = new Set(identities);
	return authMethods.filter((method) => method !== 'password' && !linked.has(method));
}

// The last way in cannot go: an account with no credential at all can only be
// recovered offline, so the unlink control disappears with the second-to-last
// identity rather than being offered and refused.
export function canUnlink(identities: string[]): boolean {
	return identities.length > 1;
}

// The server's DELETE /api/v1/account/identities/{provider} answers 404 for
// anything outside its own unlinkable list, and a password is not on it: the
// password row never offers an Unlink the request would refuse outright.
export function canUnlinkIdentity(identity: string, identities: string[]): boolean {
	if (identity === 'password') return false;
	return canUnlink(identities);
}

// The server hands back an address to send the browser to, and the browser
// will follow a redirect whatever it says: only https anywhere and http on
// this very origin (local development) are followed. Everything else --
// another script's scheme, another origin over plain http, or a string that
// is not an address at all -- is answered with the generic failure instead.
export function isFollowableRedirect(redirect: string, pageOrigin: string): boolean {
	let parsed: URL;
	try {
		parsed = new URL(redirect);
	} catch {
		return false;
	}
	// A provider's authorize address never carries credentials; one that does
	// is built to look like one host while naming another.
	if (parsed.username !== '' || parsed.password !== '') return false;
	if (parsed.protocol === 'https:') return true;
	return parsed.protocol === 'http:' && parsed.origin === pageOrigin;
}

// Signing in again has to land back on this view, so the login page needs the
// app address with the view query still attached. The path itself comes from
// the router, the same way every other /login link in the studio is built.
export function signInAgainHref(): string {
	return resolve('/login') + '?next=' + encodeURIComponent('/app?view=account');
}
