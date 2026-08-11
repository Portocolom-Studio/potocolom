import type { Generation } from '$lib/studio.svelte';

// One million CSS pixels leaves ample canvas room while bounding persisted transforms.
export const LINEAGE_WORLD_LIMIT = 1_000_000;

type CachedNode = {
	id: string;
	data: {
		output_asset_ids?: string[];
		entry: { job_id: string | null; asset_id: string };
	};
};

export function clampLineageCoordinate(value: number): number {
	return Math.min(LINEAGE_WORLD_LIMIT, Math.max(-LINEAGE_WORLD_LIMIT, value));
}

export function retainedLineageTreeOffsets<T>(
	offsets: Record<string, T>,
	loadedRootIds: ReadonlySet<string>,
	rootsAreFiltered: boolean
): Record<string, T> {
	if (rootsAreFiltered) return offsets;
	return Object.fromEntries(
		Object.entries(offsets).filter(([rootId]) => loadedRootIds.has(rootId))
	) as Record<string, T>;
}

export function decideLineageLiveArrival(
	isRoot: boolean,
	starredOnly: boolean,
	rootIsStarred: boolean
): 'insert-root' | 'inspect-descendant' | 'ignore' {
	if (!isRoot) return 'inspect-descendant';
	return !starredOnly || rootIsStarred ? 'insert-root' : 'ignore';
}

export function shouldDimLineageEdge(
	selectedNodeExists: boolean,
	edgeIsOnSelectedPath: boolean
): boolean {
	return selectedNodeExists && !edgeIsOnSelectedPath;
}

export type OptimisticStarMutation = {
	id: string;
	wasStarred: boolean;
	previousIndex: number;
};

export function beginOptimisticStarMutation(
	starredIds: string[],
	id: string
): { starredIds: string[]; mutation: OptimisticStarMutation } {
	const previousIndex = starredIds.indexOf(id);
	const wasStarred = previousIndex !== -1;
	return {
		starredIds: wasStarred
			? starredIds.filter((starredId) => starredId !== id)
			: [id, ...starredIds],
		mutation: { id, wasStarred, previousIndex }
	};
}

export function rollbackOptimisticStarMutation(
	starredIds: string[],
	mutation: OptimisticStarMutation
): string[] {
	if (!mutation.wasStarred) {
		return starredIds.filter((starredId) => starredId !== mutation.id);
	}
	if (starredIds.includes(mutation.id)) return starredIds;
	const restored = [...starredIds];
	restored.splice(Math.min(mutation.previousIndex, restored.length), 0, mutation.id);
	return restored;
}

export function starredListSnapshotIsCurrent(
	requestEpoch: number,
	currentRequestEpoch: number,
	mutationEpoch: number,
	currentMutationEpoch: number,
	mutationPending: boolean
): boolean {
	return (
		requestEpoch === currentRequestEpoch &&
		mutationEpoch === currentMutationEpoch &&
		!mutationPending
	);
}

export function settleStarredListMutation(
	dirty: boolean,
	mutationSucceeded: boolean,
	pendingMutations: number
): { dirty: boolean; reload: boolean } {
	const nextDirty = dirty || mutationSucceeded;
	return {
		dirty: nextDirty,
		reload: nextDirty && pendingMutations === 0
	};
}

export type LineageRootStarReconciliation = {
	pending: number;
	dirty: boolean;
};

export function settleLineageRootStarReconciliation(
	state: LineageRootStarReconciliation,
	mutationSucceeded: boolean,
	starredOnly: boolean
): { state: LineageRootStarReconciliation; reload: boolean } {
	const pending = state.pending - 1;
	const dirty = state.dirty || mutationSucceeded;
	if (pending > 0) return { state: { pending, dirty }, reload: false };
	return {
		state: { pending: 0, dirty: false },
		reload: dirty && starredOnly
	};
}

export function rebaseLineageViewport(
	viewport: { translateX: number; translateY: number; scale: number },
	storedAnchor: { x: number; y: number },
	currentAnchor: { x: number; y: number }
): { translateX: number; translateY: number } {
	return {
		translateX: clampLineageCoordinate(
			viewport.translateX + (storedAnchor.x - currentAnchor.x) * viewport.scale
		),
		translateY: clampLineageCoordinate(
			viewport.translateY + (storedAnchor.y - currentAnchor.y) * viewport.scale
		)
	};
}

export type InitialLineageViewportAnchor = {
	rootId: string;
	x: number;
	y: number;
};

export type InitialLineageRootLoadState = 'loading' | 'failed' | 'settled';

/** Follow the root chosen by automatic centring while one paging burst repacks
 * the forest. Retriable failures keep the anchor for the next attempt. A
 * settled burst drops it only after applying the final position, or asks the
 * caller to recenter when the anchored root has expired. */
export function decideInitialLineageViewportFollow(
	viewport: { translateX: number; translateY: number; scale: number },
	storedAnchor: InitialLineageViewportAnchor,
	currentAnchor: { x: number; y: number } | null,
	rootLoadState: InitialLineageRootLoadState
): {
	translateX: number;
	translateY: number;
	anchor: InitialLineageViewportAnchor | null;
	fallbackToNewest: boolean;
} {
	const retainAnchor = rootLoadState !== 'settled';
	if (currentAnchor === null) {
		return {
			translateX: viewport.translateX,
			translateY: viewport.translateY,
			anchor: retainAnchor ? storedAnchor : null,
			fallbackToNewest: !retainAnchor
		};
	}

	const rebased = rebaseLineageViewport(viewport, storedAnchor, currentAnchor);
	const anchor =
		storedAnchor.x === currentAnchor.x && storedAnchor.y === currentAnchor.y
			? storedAnchor
			: { rootId: storedAnchor.rootId, ...currentAnchor };
	return {
		...rebased,
		anchor: retainAnchor ? anchor : null,
		fallbackToNewest: false
	};
}

export type LineageTreeLoadState = {
	status: 'loading' | 'loaded' | 'error';
	retried?: boolean;
};

/** What the visibility pass should do with one packed tree.
 *
 * `synthesize` means cache the single-node layout locally: a root the history
 * page already reported as having no derivatives has nothing to fetch.
 * `retry` means spend this failure's one retry and load again. The budget lives
 * on the entry, and every load replaces the entry, so a later failure gets its
 * own retry rather than inheriting an exhausted one. */
export function decideLineageTreeLoad(
	hasDerivatives: boolean,
	cached: LineageTreeLoadState | undefined
): 'skip' | 'synthesize' | 'load' | 'retry' {
	if (!hasDerivatives) return cached === undefined ? 'synthesize' : 'skip';
	if (cached === undefined) return 'load';
	if (cached.status === 'loading' || cached.status === 'loaded') return 'skip';
	return cached.retried === true ? 'skip' : 'retry';
}

/** The retry budget a new load attempt inherits.
 *
 * Every load replaces the cache entry, so the budget has to be carried across
 * the loading and error transitions or an automatic retry would hand itself a
 * fresh budget on failure and loop forever. A forced load is a new request
 * rather than a continuation, so it starts with its own budget, and a
 * successful load stores no budget at all, which resets it. */
export function retainedRetryBudget(
	force: boolean,
	existing: { retried?: boolean } | undefined
): boolean | undefined {
	return force ? undefined : existing?.retried;
}

export function lineageTreeNeedsHistoryRefresh(
	nodes: CachedNode[],
	history: Generation[],
	knownOmittedJobIds: ReadonlySet<string> = new Set()
): boolean {
	const nodeByJob = new Map(
		nodes
			.filter((node) => node.data.entry.job_id !== null)
			.map((node) => [node.data.entry.job_id as string, node])
	);
	const assetIds = new Set(
		nodes.flatMap((node) => node.data.output_asset_ids ?? [node.data.entry.asset_id])
	);
	const historyAssetOwners = new Map(
		history.flatMap((generation) =>
			generation.assets.map((asset) => [asset.id, generation.id] as const)
		)
	);

	for (const generation of history) {
		if (generation.assets.length === 0) continue;
		const cached = nodeByJob.get(generation.id);
		if (cached && !generation.assets.some((asset) => asset.id === cached.data.entry.asset_id)) {
			return true;
		}
		if (!cached && generation.source_asset_id && !knownOmittedJobIds.has(generation.id)) {
			const parentJobId = historyAssetOwners.get(generation.source_asset_id);
			if (assetIds.has(generation.source_asset_id) || (parentJobId && nodeByJob.has(parentJobId))) {
				return true;
			}
		}
	}
	return false;
}

export function lineageTreeOmittedHistoryJobIds(
	nodes: CachedNode[],
	history: Generation[]
): Set<string> {
	const jobIds = new Set(
		nodes.map((node) => node.data.entry.job_id).filter((jobId): jobId is string => jobId !== null)
	);
	const assetIds = new Set(nodes.map((node) => node.id));
	const historyAssetOwners = new Map<string, string>();
	for (const generation of history) {
		for (const asset of generation.assets) historyAssetOwners.set(asset.id, generation.id);
	}
	return new Set(
		history
			.filter((generation) => {
				if (jobIds.has(generation.id) || generation.source_asset_id === null) return false;
				const parentJobId = historyAssetOwners.get(generation.source_asset_id);
				return (
					assetIds.has(generation.source_asset_id) ||
					Boolean(parentJobId && jobIds.has(parentJobId))
				);
			})
			.map((generation) => generation.id)
	);
}
