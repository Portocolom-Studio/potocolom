// Keyboard shortcuts: how a key combination is written for the platform in
// front of the user, and which realtime canvas action a key press asks for.
// Pure, so the parsing is unit tested without a DOM.

import type { DrawingTool } from './drawing-document';

/** Apple keyboards put the shortcut modifier on Command, everything else on
 * Control. */
export function isApplePlatform(platform: string): boolean {
	return /mac|iphone|ipad|ipod/i.test(platform);
}

/** One key of a combination: `mod` is Command or Control by platform. */
export type ShortcutKey = 'mod' | 'shift' | 'alt' | 'enter' | string;

const APPLE_LABELS: Record<string, string> = { mod: '⌘', shift: '⇧', alt: '⌥', enter: '↵' };
const OTHER_LABELS: Record<string, string> = {
	mod: 'Ctrl',
	shift: 'Shift',
	alt: 'Alt',
	enter: '↵'
};

/** The label a key hint shows: `⌘K` on a Mac, `Ctrl K` elsewhere. Apple
 * glyphs run together, as macOS menus print them; words take a space. */
export function formatShortcut(keys: ShortcutKey[], apple: boolean): string {
	const labels = apple ? APPLE_LABELS : OTHER_LABELS;
	const parts = keys.map((key) => labels[key] ?? (key.length === 1 ? key.toUpperCase() : key));
	return parts.join(apple ? '' : ' ');
}

/** The aria-keyshortcuts value for a combination, listing both modifiers for
 * `mod` because the handlers accept either. */
export function ariaKeyShortcuts(keys: ShortcutKey[]): string {
	const names: Record<string, string> = { shift: 'Shift', alt: 'Alt', enter: 'Enter' };
	const rest = keys
		.filter((key) => key !== 'mod')
		.map((key) => names[key] ?? (key.length === 1 ? key.toUpperCase() : key));
	if (!keys.includes('mod')) return rest.join('+');
	return ['Meta', 'Control'].map((modifier) => [modifier, ...rest].join('+')).join(' ');
}

type TargetLike = {
	closest?: (selector: string) => unknown;
	isContentEditable?: boolean;
} | null;

const TEXT_ENTRY =
	'textarea, select, [contenteditable=""], [contenteditable="true"], ' +
	'input:not([type="button"]):not([type="checkbox"]):not([type="radio"]):not([type="range"])' +
	':not([type="submit"]):not([type="reset"]):not([type="color"]):not([type="file"])';

/** Whether a key press belongs to a text field: single-key shortcuts must
 * leave typing alone. */
export function isTypingTarget(target: unknown): boolean {
	const element = target as TargetLike;
	if (element === null || typeof element !== 'object') return false;
	if (element.isContentEditable) return true;
	return typeof element.closest === 'function' && element.closest(TEXT_ENTRY) !== null;
}

export type KeyPress = {
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	shiftKey: boolean;
};

export type CanvasShortcut =
	| { kind: 'tool'; tool: DrawingTool }
	| { kind: 'select' }
	| { kind: 'brush'; delta: number }
	| { kind: 'undo' }
	| { kind: 'redo' };

/** The single keys of the realtime tools, also shown in their tooltips. */
export const TOOL_KEYS: Record<DrawingTool, string> = {
	draw: 'b',
	erase: 'e',
	line: 'l',
	rectangle: 'r',
	ellipse: 'o'
};
export const SELECT_KEY = 's';

/** The canvas action a key press asks for, or null when it asks for none.
 * The caller decides whether the press is the canvas's at all (not typed
 * into a field, no dialog open). */
export function canvasShortcut(event: KeyPress, apple: boolean): CanvasShortcut | null {
	const mod = apple ? event.metaKey : event.ctrlKey;
	const otherMod = apple ? event.ctrlKey : event.metaKey;
	const key = event.key.toLowerCase();
	if (mod && !otherMod && !event.altKey) {
		if (key === 'z') return event.shiftKey ? { kind: 'redo' } : { kind: 'undo' };
		if (key === 'y' && !apple && !event.shiftKey) return { kind: 'redo' };
		return null;
	}
	if (event.metaKey || event.ctrlKey || event.altKey) return null;
	if (event.key === '[') return { kind: 'brush', delta: -1 };
	if (event.key === ']') return { kind: 'brush', delta: 1 };
	if (event.shiftKey) return null;
	if (key === SELECT_KEY) return { kind: 'select' };
	for (const [tool, toolKey] of Object.entries(TOOL_KEYS) as [DrawingTool, string][]) {
		if (key === toolKey) return { kind: 'tool', tool };
	}
	return null;
}

/** A brush size moved by a shortcut, kept on the slider's range. */
export function stepBrushSize(size: number, delta: number, min = 1, max = 32): number {
	return Math.min(max, Math.max(min, Math.round(size) + delta));
}
