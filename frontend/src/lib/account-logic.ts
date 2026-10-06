// Pure helpers for the studio account view: the one value the delete dialog
// gates on, and the sign-in link the password form needs when the session is
// too old to change a password. Kept out of the component so both have tests.

// The typed confirmation must match the account's own address, ignoring the
// whitespace a paste brings along and the capitalisation people type.
export function deleteConfirmed(typed: string, email: string): boolean {
	return typed.trim().toLowerCase() === email.trim().toLowerCase();
}

// Signing in again has to land back on this view, so the login page needs the
// app address with the view query still attached.
export function signInAgainHref(): string {
	return '/login?next=' + encodeURIComponent('/app?view=account');
}
