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
