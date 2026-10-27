import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
	ariaKeyShortcuts,
	canvasShortcut,
	formatShortcut,
	isApplePlatform,
	isTypingTarget,
	stepBrushSize,
	type KeyPress
} from './shortcuts.ts';

function press(key: string, modifiers: Partial<KeyPress> = {}): KeyPress {
	return { key, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...modifiers };
}

test('isApplePlatform recognises Mac and iOS platform strings', () => {
	assert.equal(isApplePlatform('MacIntel'), true);
	assert.equal(isApplePlatform('macOS'), true);
	assert.equal(isApplePlatform('iPhone'), true);
	assert.equal(isApplePlatform('iPad'), true);
	assert.equal(isApplePlatform('Win32'), false);
	assert.equal(isApplePlatform('Linux x86_64'), false);
	assert.equal(isApplePlatform(''), false);
});

test('formatShortcut writes Command glyphs on Apple and words elsewhere', () => {
	assert.equal(formatShortcut(['mod', 'k'], true), '⌘K');
	assert.equal(formatShortcut(['mod', 'k'], false), 'Ctrl K');
	assert.equal(formatShortcut(['shift', 'mod', 'z'], true), '⇧⌘Z');
	assert.equal(formatShortcut(['mod', 'shift', 'z'], false), 'Ctrl Shift Z');
	assert.equal(formatShortcut(['mod', 'enter'], false), 'Ctrl ↵');
	assert.equal(formatShortcut(['['], false), '[');
	assert.equal(formatShortcut(['b'], true), 'B');
});

test('ariaKeyShortcuts lists both modifiers for mod', () => {
	assert.equal(ariaKeyShortcuts(['mod', 'k']), 'Meta+K Control+K');
	assert.equal(ariaKeyShortcuts(['mod', 'shift', 'z']), 'Meta+Shift+Z Control+Shift+Z');
	assert.equal(ariaKeyShortcuts(['b']), 'B');
	assert.equal(ariaKeyShortcuts(['mod', 'enter']), 'Meta+Enter Control+Enter');
});

test('isTypingTarget is true inside text fields only', () => {
	const inside = (selector: string) => ({
		closest: (query: string) => (query.includes(selector) ? {} : null)
	});
	assert.equal(isTypingTarget(inside('textarea')), true);
	assert.equal(isTypingTarget({ isContentEditable: true }), true);
	assert.equal(isTypingTarget({ closest: () => null }), false);
	assert.equal(isTypingTarget(null), false);
	assert.equal(isTypingTarget(undefined), false);
	assert.equal(isTypingTarget('input'), false);
});

test('isTypingTarget leaves buttons, checkboxes and ranges to shortcuts', () => {
	// The selector it asks for excludes the non-text input types.
	let asked = '';
	isTypingTarget({
		closest: (query: string) => {
			asked = query;
			return null;
		}
	});
	for (const type of ['button', 'checkbox', 'radio', 'range', 'submit']) {
		assert.match(asked, new RegExp(`:not\\(\\[type="${type}"\\]\\)`));
	}
	assert.match(asked, /textarea/);
	assert.match(asked, /select/);
});

test('canvasShortcut maps the tool letters', () => {
	assert.deepEqual(canvasShortcut(press('b'), false), { kind: 'tool', tool: 'draw' });
	assert.deepEqual(canvasShortcut(press('E'), false), { kind: 'tool', tool: 'erase' });
	assert.deepEqual(canvasShortcut(press('l'), true), { kind: 'tool', tool: 'line' });
	assert.deepEqual(canvasShortcut(press('r'), false), { kind: 'tool', tool: 'rectangle' });
	assert.deepEqual(canvasShortcut(press('o'), false), { kind: 'tool', tool: 'ellipse' });
	assert.deepEqual(canvasShortcut(press('s'), false), { kind: 'select' });
	assert.equal(canvasShortcut(press('x'), false), null);
});

test('canvasShortcut ignores letters held with a modifier', () => {
	assert.equal(canvasShortcut(press('b', { ctrlKey: true }), false), null);
	assert.equal(canvasShortcut(press('b', { metaKey: true }), true), null);
	assert.equal(canvasShortcut(press('b', { altKey: true }), false), null);
	assert.equal(canvasShortcut(press('B', { shiftKey: true }), false), null);
	assert.equal(canvasShortcut(press('r', { metaKey: true }), true), null);
});

test('canvasShortcut maps the bracket keys to brush size', () => {
	assert.deepEqual(canvasShortcut(press('['), false), { kind: 'brush', delta: -1 });
	assert.deepEqual(canvasShortcut(press(']'), true), { kind: 'brush', delta: 1 });
	assert.equal(canvasShortcut(press('[', { ctrlKey: true }), false), null);
});

test('canvasShortcut uses the platform modifier for undo and redo', () => {
	assert.deepEqual(canvasShortcut(press('z', { metaKey: true }), true), { kind: 'undo' });
	assert.deepEqual(canvasShortcut(press('Z', { metaKey: true, shiftKey: true }), true), {
		kind: 'redo'
	});
	assert.deepEqual(canvasShortcut(press('z', { ctrlKey: true }), false), { kind: 'undo' });
	assert.deepEqual(canvasShortcut(press('z', { ctrlKey: true, shiftKey: true }), false), {
		kind: 'redo'
	});
	assert.deepEqual(canvasShortcut(press('y', { ctrlKey: true }), false), { kind: 'redo' });
	// The other platform's modifier is not the shortcut.
	assert.equal(canvasShortcut(press('z', { ctrlKey: true }), true), null);
	assert.equal(canvasShortcut(press('z', { metaKey: true }), false), null);
	assert.equal(canvasShortcut(press('y', { metaKey: true }), true), null);
	assert.equal(canvasShortcut(press('z', { ctrlKey: true, altKey: true }), false), null);
});

test('stepBrushSize stays on the slider range', () => {
	assert.equal(stepBrushSize(6, 1), 7);
	assert.equal(stepBrushSize(6, -1), 5);
	assert.equal(stepBrushSize(1, -1), 1);
	assert.equal(stepBrushSize(32, 1), 32);
	assert.equal(stepBrushSize(5.6, 0), 6);
});
