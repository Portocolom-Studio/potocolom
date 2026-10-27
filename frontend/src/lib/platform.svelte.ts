// Which keyboard the studio is talking to, for shortcut hints. English is
// prerendered with the non-Apple labels; detectPlatform runs after hydration
// and every hint relabels itself from this state.
import { formatShortcut, isApplePlatform, type ShortcutKey } from './shortcuts';

export const platform = $state({ apple: false });

export function detectPlatform(): void {
	try {
		const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
		platform.apple = isApplePlatform(nav.userAgentData?.platform || nav.platform || '');
	} catch {
		platform.apple = false;
	}
}

/** A key hint in the current platform's spelling, such as ⌘K or Ctrl K. */
export function shortcutLabel(keys: ShortcutKey[]): string {
	return formatShortcut(keys, platform.apple);
}
