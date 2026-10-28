import { accountOwnerId, parseAccount, type Role } from './account-display';
import { apiFetch } from './api';

export type AccountProbeResult =
	| { kind: 'account'; current: { email: string; role: Role }; ownerId: string }
	| { kind: 'local' }
	| { kind: 'signed_out' }
	| { kind: 'unknown'; current: { email: string; role: Role } | null }
	| { kind: 'failed' };

const PROBE_TIMEOUT_MS = 5_000;
const failed: AccountProbeResult = { kind: 'failed' };

async function readProbe(signal: AbortSignal): Promise<AccountProbeResult> {
	const response = await apiFetch('/api/v1/account', { signal });
	if (response.status === 401) return { kind: 'signed_out' };
	if (response.status === 404) {
		const configResponse = await apiFetch('/api/v1/config', { signal });
		if (configResponse.status !== 200) return failed;
		const config: unknown = await configResponse.json();
		if (typeof config !== 'object' || config === null) return { kind: 'unknown', current: null };
		const methods = (config as { auth_methods?: unknown }).auth_methods;
		if (!Array.isArray(methods)) return { kind: 'unknown', current: null };
		return methods.length === 0 ? { kind: 'local' } : { kind: 'unknown', current: null };
	}
	if (response.status !== 200) return failed;

	const body: unknown = await response.json();
	const current = parseAccount(body);
	if (!current) return { kind: 'unknown', current: null };
	const ownerId = accountOwnerId(body);
	return ownerId ? { kind: 'account', current, ownerId } : { kind: 'unknown', current };
}

/** Read and classify the current account without changing application state. */
export async function probeAccount(signal?: AbortSignal): Promise<AccountProbeResult> {
	if (signal?.aborted) return failed;
	const controller = new AbortController();
	const abortFromCaller = () => controller.abort();
	let resolveCallerCancel: (() => void) | undefined;
	signal?.addEventListener('abort', abortFromCaller, { once: true });
	let timeout: ReturnType<typeof setTimeout> | undefined;
	const deadline = new Promise<AccountProbeResult>((resolve) => {
		timeout = setTimeout(() => {
			controller.abort();
			resolve(failed);
		}, PROBE_TIMEOUT_MS);
	});
	const callerCancelled = new Promise<AccountProbeResult>((resolve) => {
		if (signal) {
			resolveCallerCancel = () => resolve(failed);
			signal.addEventListener('abort', resolveCallerCancel, { once: true });
		}
	});
	try {
		return await Promise.race([readProbe(controller.signal), deadline, callerCancelled]);
	} catch {
		return failed;
	} finally {
		if (timeout !== undefined) clearTimeout(timeout);
		signal?.removeEventListener('abort', abortFromCaller);
		if (resolveCallerCancel) signal?.removeEventListener('abort', resolveCallerCancel);
	}
}
