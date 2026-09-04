export type PromptDiffToken = {
	kind: 'same' | 'added' | 'removed';
	text: string;
};

export type ParamDelta =
	| { kind: 'changed'; key: string; from: string; to: string }
	| { kind: 'added'; key: string; value: string }
	| { kind: 'removed'; key: string; value: string }
	| { kind: 'new-seed' };

function words(value: string): string[] {
	return value.trim().split(/\s+/).filter(Boolean);
}

function merge(tokens: PromptDiffToken[]): PromptDiffToken[] {
	const merged: PromptDiffToken[] = [];
	for (const token of tokens) {
		const last = merged[merged.length - 1];
		if (last && last.kind === token.kind) {
			last.text = `${last.text} ${token.text}`;
		} else {
			merged.push({ ...token });
		}
	}
	return merged;
}

/** Word-level LCS so added and removed terms stay readable without color. */
export function promptWordDiff(parent: string, child: string): PromptDiffToken[] {
	const left = words(parent);
	const right = words(child);
	const rows = left.length;
	const cols = right.length;
	const table: number[][] = Array.from({ length: rows + 1 }, () => Array<number>(cols + 1).fill(0));
	for (let row = 1; row <= rows; row += 1) {
		const prev = table[row - 1] ?? [];
		const current = table[row] ?? [];
		for (let col = 1; col <= cols; col += 1) {
			current[col] =
				left[row - 1] === right[col - 1]
					? (prev[col - 1] ?? 0) + 1
					: Math.max(prev[col] ?? 0, current[col - 1] ?? 0);
		}
	}
	const tokens: PromptDiffToken[] = [];
	let row = rows;
	let col = cols;
	while (row > 0 && col > 0) {
		const leftWord = left[row - 1];
		const rightWord = right[col - 1];
		if (leftWord === rightWord && leftWord !== undefined) {
			tokens.push({ kind: 'same', text: leftWord });
			row -= 1;
			col -= 1;
			continue;
		}
		const up = table[row - 1]?.[col] ?? 0;
		const side = table[row]?.[col - 1] ?? 0;
		if (up > side && leftWord !== undefined) {
			tokens.push({ kind: 'removed', text: leftWord });
			row -= 1;
		} else if (rightWord !== undefined) {
			tokens.push({ kind: 'added', text: rightWord });
			col -= 1;
		} else {
			break;
		}
	}
	while (row > 0) {
		const leftWord = left[row - 1];
		if (leftWord !== undefined) tokens.push({ kind: 'removed', text: leftWord });
		row -= 1;
	}
	while (col > 0) {
		const rightWord = right[col - 1];
		if (rightWord !== undefined) tokens.push({ kind: 'added', text: rightWord });
		col -= 1;
	}
	return merge(tokens.reverse());
}

/** Keep a few same-words around each change so a long prompt stays readable. */
export function collapsePromptDiff(
	tokens: PromptDiffToken[],
	context = 2
): { tokens: PromptDiffToken[]; truncated: boolean } {
	const exploded = tokens.flatMap((token) =>
		words(token.text).map((text) => ({ kind: token.kind, text }))
	);
	if (exploded.every((token) => token.kind === 'same') || exploded.length === 0) {
		return { tokens, truncated: false };
	}
	const keep = new Set<number>();
	for (const [index, token] of exploded.entries()) {
		if (token.kind === 'same') continue;
		for (let offset = -context; offset <= context; offset += 1) {
			const neighbour = index + offset;
			if (neighbour >= 0 && neighbour < exploded.length) keep.add(neighbour);
		}
		keep.add(index);
	}
	const collapsed: PromptDiffToken[] = [];
	let truncated = false;
	for (const [index, token] of exploded.entries()) {
		if (keep.has(index)) {
			collapsed.push(token);
			continue;
		}
		truncated = true;
	}
	return { tokens: merge(collapsed), truncated };
}

function displayValue(value: unknown): string {
	if (typeof value === 'string') return value;
	if (typeof value === 'number' && Number.isFinite(value)) return String(value);
	if (typeof value === 'boolean') return value ? 'true' : 'false';
	if (value === null) return 'null';
	return JSON.stringify(value);
}

export function paramDeltas(
	parent: Record<string, unknown>,
	child: Record<string, unknown>
): ParamDelta[] {
	const keys = new Set([...Object.keys(parent), ...Object.keys(child)]);
	keys.delete('prompt');
	const deltas: ParamDelta[] = [];
	let seedChanged = false;
	for (const key of [...keys].sort()) {
		const from = parent[key];
		const to = child[key];
		if (from === to) continue;
		if (key === 'seed') {
			seedChanged = true;
			continue;
		}
		if (from === undefined) {
			deltas.push({ kind: 'added', key, value: displayValue(to) });
		} else if (to === undefined) {
			deltas.push({ kind: 'removed', key, value: displayValue(from) });
		} else if (displayValue(from) !== displayValue(to)) {
			deltas.push({ kind: 'changed', key, from: displayValue(from), to: displayValue(to) });
		}
	}
	if (seedChanged) deltas.push({ kind: 'new-seed' });
	return deltas;
}
