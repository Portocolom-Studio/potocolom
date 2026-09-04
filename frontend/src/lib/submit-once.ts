/** One in-flight POST at a time. A second click is ignored, not queued. */

export type SubmitLock = { busy: boolean };

export async function runExclusive(lock: SubmitLock, work: () => Promise<void>): Promise<boolean> {
	if (lock.busy) return false;
	lock.busy = true;
	try {
		await work();
		return true;
	} finally {
		lock.busy = false;
	}
}
