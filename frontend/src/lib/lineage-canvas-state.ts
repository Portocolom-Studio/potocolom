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

export function lineageTreeNeedsHistoryRefresh(
	nodes: CachedNode[],
	history: Generation[]
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
		if (!cached && generation.source_asset_id) {
			const parentJobId = historyAssetOwners.get(generation.source_asset_id);
			if (assetIds.has(generation.source_asset_id) || (parentJobId && nodeByJob.has(parentJobId))) {
				return true;
			}
		}
	}
	return false;
}
