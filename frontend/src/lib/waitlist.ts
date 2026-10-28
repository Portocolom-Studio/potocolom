/** What a waitlist signup came back with. */
export type WaitlistResult = 'done' | 'already' | 'error';

/**
 * Posts an address to the waitlist endpoint baked in at build time
 * (PUBLIC_WAITLIST_URL). The endpoint answers JSON with status "exists" when
 * the address is already on the list; anything else that is not a JSON 2xx
 * counts as a failure.
 */
export async function joinWaitlist(
	endpoint: string,
	email: string,
	locale: string,
	fetcher: typeof fetch = fetch
): Promise<WaitlistResult> {
	try {
		const response = await fetcher(endpoint, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ email, locale }),
			// A stalled endpoint must not leave the form disabled; the catch below
			// turns the abort into the retry state.
			signal: AbortSignal.timeout(15_000)
		});
		const contentType = response.headers.get('content-type') ?? '';
		if (!response.ok || !contentType.includes('application/json')) return 'error';
		const result = (await response.json()) as { status?: string };
		return result.status === 'exists' ? 'already' : 'done';
	} catch {
		return 'error';
	}
}

/**
 * The /login tab to open: the one ?tab= names, the waitlist only when there is
 * one, and otherwise sign-in, except on the landing build, where sign-in is
 * unavailable and the waitlist leads.
 */
export function initialAuthTab(
	search: string,
	waitlist: boolean,
	landing = false
): 'signin' | 'waitlist' {
	if (!waitlist) return 'signin';
	const asked = new URLSearchParams(search).get('tab');
	if (asked === 'waitlist' || asked === 'signin') return asked;
	return landing ? 'waitlist' : 'signin';
}
