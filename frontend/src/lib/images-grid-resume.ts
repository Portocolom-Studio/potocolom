import type { Generation } from './studio.svelte.ts';

export const MAX_GRID_VISIT_ROWS = 1000;
export const GRID_RESUME_LIMIT = 200;

export type GridResumeAnchor = {
	id: string;
	beforeCursor: string | null;
	offset: number;
};

export type GridVisitSnapshot = {
	criteriaKey: string;
	rows: Generation[];
	beforeCursor: string | null;
	cursor: string | null;
	hasMore: boolean;
	window: 'newest' | 'cursor';
	provenance: 'server-success';
};

export function imagesGridSnapshotKey(ownerEpoch: number): string {
	return `images:grid:${ownerEpoch}`;
}

export function snapshotMatchesCriteria(
	snapshot: GridVisitSnapshot | undefined,
	criteriaKey: string
): snapshot is GridVisitSnapshot {
	return snapshot?.provenance === 'server-success' && snapshot.criteriaKey === criteriaKey;
}

export function makeGridVisitSnapshot(args: {
	criteriaKey: string;
	rows: Generation[];
	beforeCursor: string | null;
	hasMore: boolean;
	window: 'newest' | 'cursor';
}): GridVisitSnapshot {
	const rows = args.rows.slice(0, MAX_GRID_VISIT_ROWS);
	const trimmed = rows.length < args.rows.length;
	return {
		criteriaKey: args.criteriaKey,
		rows,
		beforeCursor: args.beforeCursor,
		cursor: rows.at(-1)?.id ?? null,
		hasMore: trimmed || args.hasMore,
		window: args.window,
		provenance: 'server-success'
	};
}

export function restoredGridAnchor(
	rows: Generation[],
	anchor: GridResumeAnchor | null
): GridResumeAnchor | null {
	if (anchor === null || !rows.some((row) => row.id === anchor.id)) return null;
	return anchor;
}

export function selectionInGridWindow(
	rows: Generation[],
	selectedId: string | null
): string | null {
	return selectedId !== null && rows.some((row) => row.id === selectedId) ? selectedId : null;
}

export function mayPrependLiveGridRows(window: 'newest' | 'cursor'): boolean {
	return window === 'newest';
}

export function rowsForSnapshot(snapshot: GridVisitSnapshot, criteriaKey: string): Generation[] {
	return snapshotMatchesCriteria(snapshot, criteriaKey) ? snapshot.rows : [];
}
