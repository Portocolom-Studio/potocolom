// The localStorage property access itself throws (a SecurityError) where the
// browser blocks site data, so a typeof guard is not enough; on the server the
// identifier does not exist and throws a ReferenceError, caught the same way.

export function readStored(key: string): string | null {
	try {
		return localStorage.getItem(key);
	} catch {
		return null;
	}
}

export function writeStored(key: string, value: string): void {
	try {
		localStorage.setItem(key, value);
	} catch {}
}

export function removeStored(key: string): void {
	try {
		localStorage.removeItem(key);
	} catch {}
}
