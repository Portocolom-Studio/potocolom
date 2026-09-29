// $app/navigation stand-in: a test reads the recorded calls instead of a
// router it does not have.

export type NavigationCall = {
	url: string;
	options: Record<string, unknown> | undefined;
};

export const navigationCalls: NavigationCall[] = [];

export function resetNavigationCalls(): void {
	navigationCalls.length = 0;
}

export function goto(url: string, options?: Record<string, unknown>): Promise<void> {
	navigationCalls.push({ url, options });
	return Promise.resolve();
}

export function replaceState(url: string, options?: Record<string, unknown>): void {
	navigationCalls.push({ url, options });
}
