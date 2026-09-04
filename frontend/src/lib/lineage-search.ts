export function lineageSearchTone(
	jobId: string | null,
	matchIds: ReadonlySet<string> | null
): 'plain' | 'match' | 'dim' {
	if (matchIds === null) return 'plain';
	if (jobId !== null && matchIds.has(jobId)) return 'match';
	return 'dim';
}

export function nextLineageMatchIndex(current: number, total: number): number {
	if (total <= 0) return 0;
	return (current + 1 + total) % total;
}

type SearchNode = {
	data: { entry: { job_id: string | null } };
	x: number;
	y: number;
};

type SearchTree = {
	rootId: string;
	x: number;
	y: number;
	layout: { nodes: SearchNode[] };
};

export function lineageSearchMatchPositions(
	trees: readonly SearchTree[],
	matchIds: ReadonlySet<string>
): { jobId: string; x: number; y: number }[] {
	const hits: { jobId: string; x: number; y: number }[] = [];
	for (const tree of trees) {
		for (const node of tree.layout.nodes) {
			const jobId = node.data.entry.job_id;
			if (jobId !== null && matchIds.has(jobId)) {
				hits.push({ jobId, x: tree.x + node.x, y: tree.y + node.y });
			}
		}
	}
	return hits;
}

export function lineageSearchMatchRootIds(
	trees: readonly SearchTree[],
	matchIds: ReadonlySet<string> | null
): ReadonlySet<string> {
	const roots = new Set<string>();
	if (matchIds === null) return roots;
	for (const tree of trees) {
		if (
			tree.layout.nodes.some(
				(node) => node.data.entry.job_id !== null && matchIds.has(node.data.entry.job_id)
			)
		) {
			roots.add(tree.rootId);
		}
	}
	return roots;
}
