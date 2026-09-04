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

export function shouldShowChallenge(view: AuthView): boolean {
	return view === 'challenge';
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
		return { kind: 'rate_limited', message: detail || 'too many sign-in attempts', retryAfterSeconds };
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
