import { resolve } from '$app/paths';
import type en from './i18n/en.json';

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

// The account view's refusals, as the key that says each one in the reader's
// language. A detail that differs in any way is not one this build
// translates, and is shown as the server spelled it.
const REFUSALS: Readonly<Record<string, keyof typeof en>> = {
	'that address is already in use': 'app.account.refusal_address_taken',
	'invalid email address': 'app.account.refusal_invalid_email',
	'that is the only way in': 'app.account.refusal_only_way_in',
	'that code is not valid': 'app.account.refusal_code_invalid',
	'the current password is required': 'app.account.refusal_current_password',
	'password does not meet the policy': 'app.account.refusal_password_policy',
	'this account already has a password': 'app.account.refusal_has_password',
	'a second factor was enrolled already': 'app.account.refusal_factor_exists',
	'this session changed while that was in flight': 'app.account.refusal_session_changed',
	'too many changes to this account at once': 'app.account.refusal_busy',
	'account suspended': 'app.account.refusal_suspended'
};

// The key that says this refusal in the reader's language, or null for a
// detail this build does not translate, which the caller shows as sent.
// The table is asked whether the detail is its own before it is read, so a
// detail naming something the object inherits ("toString") finds nothing
// there to translate.
export function refusalKey(detail: unknown): keyof typeof en | null {
	if (typeof detail !== 'string') return null;
	if (!Object.hasOwn(REFUSALS, detail)) return null;
	return REFUSALS[detail];
}

// The two authorize hosts the backend builds its provider links from. A link
// redirect is followed only to one of them, so an answer naming any other
// host stops the navigation instead of taking the browser there.
const PROVIDERS: ReadonlySet<string> = new Set([
	'https://accounts.google.com',
	'https://github.com'
]);

// The server hands back an address to send the browser to, and the browser
// will follow a redirect whatever it says: the two provider authorize hosts
// and http on this very origin (local development) are the only addresses
// this navigation will take. Everything else -- another host, another
// script's scheme, or a string that is not an address at all -- is answered
// with the generic failure instead.
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
	if (parsed.protocol === 'https:') return PROVIDERS.has(parsed.origin);
	return parsed.protocol === 'http:' && parsed.origin === pageOrigin;
}

// Signing in again has to land back on this view, so the login page needs the
// app address with the view query still attached. The path itself comes from
// the router, the same way every other /login link in the studio is built.
export function signInAgainHref(): string {
	return resolve('/login') + '?next=' + encodeURIComponent('/app?view=account');
}
