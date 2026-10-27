// Stale-while-revalidate, small: a view that is unmounted and opened again
// paints what it showed last time at once and refreshes behind it, instead
// of starting from a spinner. In memory only, so a reload starts clean and
// nothing outlives the tab.

export type CacheEntry<T> = { value: T; at: number };

const ACCOUNT_PREFIX = 'account:';

/** The key for data owned by one validated account UUID. */
export function accountCacheKey(owner: string | null, key: string): string {
	return `${ACCOUNT_PREFIX}${owner ?? ''}:${key}`;
}

export class SwrCache {
	#entries = new Map<string, CacheEntry<unknown>>();

	/** The last value written under key, however old. */
	read<T>(key: string): T | undefined {
		return this.#entries.get(key)?.value as T | undefined;
	}

	write<T>(key: string, value: T, now = Date.now()): void {
		this.#entries.set(key, { value, at: now });
	}

	/** Milliseconds since key was written, or null when it never was. */
	age(key: string, now = Date.now()): number | null {
		const entry = this.#entries.get(key);
		return entry === undefined ? null : Math.max(0, now - entry.at);
	}

	delete(key: string): void {
		this.#entries.delete(key);
	}

	clear(): void {
		this.#entries.clear();
	}
}

/** Whether a refresh on window focus is worth sending: never more often than
 * minIntervalMs, so switching windows back and forth stays cheap. */
export function revalidationDue(
	lastAt: number | null,
	now: number,
	minIntervalMs: number
): boolean {
	return lastAt === null || now - lastAt >= minIntervalMs;
}

/** The studio's shared cache: every view reads and writes its own keys. */
export const studioCache = new SwrCache();
