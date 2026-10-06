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

// Signing in again has to land back on this view, so the login page needs the
// app address with the view query still attached. The path itself comes from
// the router, the same way every other /login link in the studio is built.
export function signInAgainHref(): string {
	return resolve('/login') + '?next=' + encodeURIComponent('/app?view=account');
}
