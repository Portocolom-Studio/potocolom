export type AuthView = 'password' | 'challenge';

export function readInviteTokenFromHash(hash: string): string | null {
	const trimmed = hash.trim();
	if (!trimmed || trimmed === '#') return null;
	return trimmed.startsWith('#') ? trimmed.slice(1) : trimmed;
}

export function initialAuthView(search: string): AuthView {
	const params = new URLSearchParams(search);
	return params.get('totp') === 'required' ? 'challenge' : 'password';
}

// Keeps ?totp=required in sync with the on-screen view, so a reload reopens
// the challenge (initialAuthView) instead of dropping back to the password
// form. Other query params (e.g. reset=done) pass through untouched.
export function challengeSearch(search: string, inChallenge: boolean): string {
	const params = new URLSearchParams(search);
	if (inChallenge) {
		params.set('totp', 'required');
	} else {
		params.delete('totp');
	}
	const query = params.toString();
	return query ? `?${query}` : '';
}

export function resetJustHappened(search: string): boolean {
	return new URLSearchParams(search).get('reset') === 'done';
}

export function shouldShowChallenge(view: AuthView): boolean {
	return view === 'challenge';
}

// The account probe answers 401 only when accounts mode is on and the visitor
// holds no session. Anything else (200 signed in, 404 when the account routes
// are not mounted, a null status when the API did not answer) opens the studio.
export function accountCheckForcesLogin(status: number | null): boolean {
	return status === 401;
}

export function loginSearchFor(appSearch: string): string {
	if (appSearch === '' || appSearch === '?') return '';
	return '?next=' + encodeURIComponent('/app' + appSearch);
}

// Strict on purpose: the value comes from the address bar and later travels
// through a redirect, so only a same-origin /app address may be returned to.
export function studioReturnSearch(next: string | null): string {
	if (!next) return '';
	if (!next.startsWith('/app') || next.startsWith('//')) return '';
	if (next.includes('\\')) return '';
	const url = new URL(next, 'http://origin.invalid');
	if (url.origin !== 'http://origin.invalid' || url.pathname !== '/app') return '';
	return url.search;
}

export function createSubmitGuard() {
	let busy = false;

	return {
		get busy() {
			return busy;
		},
		async run<T>(fn: () => Promise<T>): Promise<T | undefined> {
			if (busy) return undefined;
			busy = true;
			try {
				return await fn();
			} finally {
				busy = false;
			}
		}
	};
}

export type AuthErrorKind = 'invalid' | 'rate_limited' | 'busy' | 'policy' | 'unknown';

export type AuthError = {
	kind: AuthErrorKind;
	message: string;
	retryAfterSeconds?: number;
};

export async function parseAuthError(response: Response): Promise<AuthError> {
	const retryAfter = response.headers.get('Retry-After');
	const retryAfterSeconds = retryAfter ? Number.parseInt(retryAfter, 10) : undefined;
	let detail = '';
	try {
		const body = (await response.json()) as { detail?: string };
		detail = typeof body.detail === 'string' ? body.detail : '';
	} catch {
		detail = '';
	}

	if (response.status === 429 || detail === 'too many sign-in attempts') {
		return {
			kind: 'rate_limited',
			message: detail || 'too many sign-in attempts',
			retryAfterSeconds
		};
	}
	if (response.status === 503 || detail === 'sign-in is busy, try again shortly') {
		return {
			kind: 'busy',
			message: detail || 'sign-in is busy, try again shortly',
			retryAfterSeconds
		};
	}
	if (response.status === 403 && detail === 'that code is not valid') {
		return { kind: 'invalid', message: detail };
	}
	if (response.status === 400 && detail === 'password does not meet the policy') {
		return { kind: 'policy', message: detail };
	}
	if (response.status === 401 || response.status === 403) {
		return { kind: 'invalid', message: detail || 'invalid email or password' };
	}
	return { kind: 'unknown', message: detail || 'request failed' };
}
