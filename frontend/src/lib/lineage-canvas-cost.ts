import {
	layoutLineageTree,
	lineageEdgePath,
	packLineageForest,
	rectsIntersect,
	viewportWorldRect,
	type LineageLayoutNode,
	type PackedLineageTree
} from './lineage-layout.ts';

export type CanvasCostReport = {
	nodeCount: number;
	treeCount: number;
	edgeCount: number;
	visibleEdgeCount: number;
	allVisibleEdgeCount: number;
	frames: number;
	edgeIfMs: number;
	edgeEachMs: number;
	viewportMs: number;
	edgeIfAllVisibleMs: number;
	edgeEachAllVisibleMs: number;
	edgeIfPerFrameMs: number;
	edgeEachPerFrameMs: number;
	viewportPerFrameMs: number;
	edgeIfAllVisiblePerFrameMs: number;
	edgeEachAllVisiblePerFrameMs: number;
};

type BenchData = { action: string };

function bush(prefix: string, remaining: number, createdAt: string): LineageLayoutNode<BenchData> {
	const id = prefix;
	if (remaining <= 1) {
		return { id, createdAt, data: { action: 'generate' }, children: [] };
	}
	const leftSize = Math.floor((remaining - 1) / 2);
	const rightSize = remaining - 1 - leftSize;
	const children: LineageLayoutNode<BenchData>[] = [];
	if (leftSize > 0) children.push(bush(`${prefix}L`, leftSize, createdAt));
	if (rightSize > 0) children.push(bush(`${prefix}R`, rightSize, createdAt));
	return { id, createdAt, data: { action: 'image_to_image' }, children };
}

export function buildSyntheticForest(
	nodeCount: number,
	treeCount: number
): PackedLineageTree<BenchData>[] {
	const perTree = Math.max(1, Math.floor(nodeCount / treeCount));
	const layouts = Array.from({ length: treeCount }, (_, index) => {
		const createdAt = new Date(1_700_000_000_000 - index * 1000).toISOString();
		const root = bush(`t${index}`, perTree, createdAt);
		return {
			rootId: root.id,
			createdAt,
			hasDerivatives: perTree > 1,
			layout: layoutLineageTree(root)
		};
	});
	return packLineageForest(layouts);
}

function edgeBounds(
	tree: PackedLineageTree<BenchData>,
	edge: PackedLineageTree<BenchData>['layout']['edges'][number]
) {
	return {
		left: tree.x + Math.min(edge.source.x, edge.target.x),
		top: tree.y + Math.min(edge.source.y, edge.target.y),
		right: tree.x + Math.max(edge.source.x, edge.target.x),
		bottom: tree.y + Math.max(edge.source.y, edge.target.y)
	};
}

function measure(frames: number, fn: () => void): number {
	const start = performance.now();
	for (let i = 0; i < frames; i += 1) fn();
	return performance.now() - start;
}

function currentEdgeIf(
	packed: PackedLineageTree<BenchData>[],
	worldRect: ReturnType<typeof viewportWorldRect>
): number {
	let visible = 0;
	for (const tree of packed) {
		for (const edge of tree.layout.edges) {
			if (rectsIntersect(worldRect, edgeBounds(tree, edge))) {
				lineageEdgePath(edge.source, edge.target, tree.x, tree.y);
				visible += 1;
			}
		}
	}
	return visible;
}

function filteredEdgeEach(
	packed: PackedLineageTree<BenchData>[],
	worldRect: ReturnType<typeof viewportWorldRect>
): number {
	const visibleEdges: {
		tree: PackedLineageTree<BenchData>;
		edge: PackedLineageTree<BenchData>['layout']['edges'][number];
	}[] = [];
	for (const tree of packed) {
		for (const edge of tree.layout.edges) {
			if (rectsIntersect(worldRect, edgeBounds(tree, edge))) {
				visibleEdges.push({ tree, edge });
			}
		}
	}
	for (const item of visibleEdges) {
		lineageEdgePath(item.edge.source, item.edge.target, item.tree.x, item.tree.y);
	}
	return visibleEdges.length;
}

function lineageViewportCost(
	packed: PackedLineageTree<BenchData>[],
	viewportWidth: number,
	viewportHeight: number,
	translateX: number,
	translateY: number,
	scale: number
): { rootId: string | null; anchorX: number | null; anchorY: number | null } {
	if (packed.length === 0) {
		return { rootId: null, anchorX: null, anchorY: null };
	}
	const centerX = (viewportWidth / 2 - translateX) / scale;
	const centerY = (viewportHeight / 2 - translateY) / scale;
	let nearest: { id: string; x: number; y: number; distance: number } | null = null;
	for (const tree of packed) {
		const rootNode = tree.layout.nodes.find((node) => node.id === tree.layout.rootId);
		if (!rootNode) continue;
		const x = tree.x + rootNode.x;
		const y = tree.y + rootNode.y;
		const distance = Math.hypot(x - centerX, y - centerY);
		if (nearest === null || distance < nearest.distance) {
			nearest = { id: tree.rootId, x, y, distance };
		}
	}
	return {
		rootId: nearest?.id ?? null,
		anchorX: nearest?.x ?? null,
		anchorY: nearest?.y ?? null
	};
}

export function measureCanvasCosts(options?: {
	nodeCount?: number;
	treeCount?: number;
	frames?: number;
}): CanvasCostReport {
	const nodeCount = options?.nodeCount ?? 3000;
	const treeCount = options?.treeCount ?? 50;
	const frames = options?.frames ?? 200;
	const packed = buildSyntheticForest(nodeCount, treeCount);
	const nodes = packed.reduce((sum, tree) => sum + tree.layout.nodes.length, 0);
	const edges = packed.reduce((sum, tree) => sum + tree.layout.edges.length, 0);
	const viewportWidth = 1440;
	const viewportHeight = 900;
	const translateX = 72;
	const translateY = 72;
	const scale = 0.35;
	const worldRect = viewportWorldRect(viewportWidth, viewportHeight, translateX, translateY, scale);
	const visibleEdgeCount = currentEdgeIf(packed, worldRect);
	const allRect = {
		left: Math.min(...packed.map((tree) => tree.x)),
		top: Math.min(...packed.map((tree) => tree.y)),
		right: Math.max(...packed.map((tree) => tree.x + tree.layout.width)),
		bottom: Math.max(...packed.map((tree) => tree.y + tree.layout.height))
	};
	const allVisibleEdgeCount = currentEdgeIf(packed, allRect);
	const edgeIfMs = measure(frames, () => {
		currentEdgeIf(packed, worldRect);
	});
	const edgeEachMs = measure(frames, () => {
		filteredEdgeEach(packed, worldRect);
	});
	const viewportMs = measure(frames, () => {
		lineageViewportCost(packed, viewportWidth, viewportHeight, translateX, translateY, scale);
	});
	const edgeIfAllVisibleMs = measure(frames, () => {
		currentEdgeIf(packed, allRect);
	});
	const edgeEachAllVisibleMs = measure(frames, () => {
		filteredEdgeEach(packed, allRect);
	});
	return {
		nodeCount: nodes,
		treeCount: packed.length,
		edgeCount: edges,
		visibleEdgeCount,
		allVisibleEdgeCount,
		frames,
		edgeIfMs,
		edgeEachMs,
		viewportMs,
		edgeIfAllVisibleMs,
		edgeEachAllVisibleMs,
		edgeIfPerFrameMs: edgeIfMs / frames,
		edgeEachPerFrameMs: edgeEachMs / frames,
		viewportPerFrameMs: viewportMs / frames,
		edgeIfAllVisiblePerFrameMs: edgeIfAllVisibleMs / frames,
		edgeEachAllVisiblePerFrameMs: edgeEachAllVisibleMs / frames
	};
}
