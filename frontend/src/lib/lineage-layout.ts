import { hierarchy, tree } from 'd3-hierarchy';

export const LINEAGE_TILE_WIDTH = 216;
export const LINEAGE_TILE_HEIGHT = 176;
const DEPTH_GAP = 96;
const SIBLING_GAP = 56;
const ROW_GAP = 240;
const GRID_GAP = 48;
// Older root pages extend the grid to the right without moving the tree band.
const GRID_ROWS = 6;

export type LineageLayoutNode<T> = {
	id: string;
	createdAt: string;
	data: T;
	children: LineageLayoutNode<T>[];
};

export type PositionedLineageNode<T> = {
	id: string;
	x: number;
	y: number;
	data: T;
};

export type PositionedLineageEdge<T> = {
	id: string;
	source: PositionedLineageNode<T>;
	target: PositionedLineageNode<T>;
};

export type LineageTreeLayout<T> = {
	rootId: string;
	nodes: PositionedLineageNode<T>[];
	edges: PositionedLineageEdge<T>[];
	width: number;
	height: number;
};

export type PackedLineageTree<T> = {
	rootId: string;
	createdAt: string;
	hasDerivatives: boolean;
	x: number;
	y: number;
	layout: LineageTreeLayout<T>;
};

export type WorldRect = {
	left: number;
	top: number;
	right: number;
	bottom: number;
};

function orderedChildren<T>(node: LineageLayoutNode<T>): LineageLayoutNode<T>[] {
	return [...node.children].sort(
		(left, right) =>
			left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id)
	);
}

export function layoutLineageTree<T>(root: LineageLayoutNode<T>): LineageTreeLayout<T> {
	const rootHierarchy = hierarchy(root, orderedChildren);
	// Trees grow downward: d3's first axis is breadth (siblings side by side, so
	// it is spaced by tile width) and its second is depth (generations stacked,
	// spaced by tile height). Coordinates are used unswapped for that reason.
	const tidy = tree<LineageLayoutNode<T>>().nodeSize([
		LINEAGE_TILE_WIDTH + SIBLING_GAP,
		LINEAGE_TILE_HEIGHT + DEPTH_GAP
	]);
	tidy(rootHierarchy);

	const raw = rootHierarchy.descendants().map((node) => ({
		id: node.data.id,
		x: node.x ?? 0,
		y: node.y ?? 0,
		data: node.data.data
	}));
	const minX = Math.min(...raw.map((node) => node.x - LINEAGE_TILE_WIDTH / 2));
	const minY = Math.min(...raw.map((node) => node.y - LINEAGE_TILE_HEIGHT / 2));
	const nodes = raw.map((node) => ({
		...node,
		x: node.x - minX,
		y: node.y - minY
	}));
	const byId = new Map(nodes.map((node) => [node.id, node]));
	const edges = rootHierarchy.links().map((link) => ({
		id: `${link.source.data.id}:${link.target.data.id}`,
		source: byId.get(link.source.data.id) as PositionedLineageNode<T>,
		target: byId.get(link.target.data.id) as PositionedLineageNode<T>
	}));

	return {
		rootId: root.id,
		nodes,
		edges,
		width: Math.max(...nodes.map((node) => node.x + LINEAGE_TILE_WIDTH / 2)),
		height: Math.max(...nodes.map((node) => node.y + LINEAGE_TILE_HEIGHT / 2))
	};
}

export function packLineageForest<T>(
	trees: {
		rootId: string;
		createdAt: string;
		hasDerivatives: boolean;
		layout: LineageTreeLayout<T>;
	}[]
): PackedLineageTree<T>[] {
	const ordered = [...trees].sort(
		(left, right) =>
			right.createdAt.localeCompare(left.createdAt) || left.rootId.localeCompare(right.rootId)
	);
	const branched = ordered.filter((item) => item.hasDerivatives);
	const singles = ordered.filter((item) => !item.hasDerivatives);
	const packed: PackedLineageTree<T>[] = [];
	const cellWidth = LINEAGE_TILE_WIDTH + GRID_GAP;
	const cellHeight = LINEAGE_TILE_HEIGHT + GRID_GAP;
	let y = GRID_ROWS * cellHeight + ROW_GAP;

	for (const item of branched) {
		const root = item.layout.nodes.find((node) => node.id === item.layout.rootId);
		packed.push({ ...item, x: LINEAGE_TILE_WIDTH / 2 - (root?.x ?? 0), y });
		y += item.layout.height + ROW_GAP;
	}

	for (const [index, item] of singles.entries()) {
		packed.push({
			...item,
			x: Math.floor(index / GRID_ROWS) * cellWidth,
			y: (index % GRID_ROWS) * cellHeight
		});
	}
	return packed;
}

export function lineageLod(scale: number): 'constellation' | 'trees' | 'cards' {
	if (scale < 0.25) return 'constellation';
	if (scale < 0.8) return 'trees';
	return 'cards';
}

export function lineageEdgePath<T>(
	source: PositionedLineageNode<T>,
	target: PositionedLineageNode<T>,
	offsetX = 0,
	offsetY = 0
): string {
	// Leaves the parent's bottom edge and arrives at the child's top edge, so the
	// curve never runs underneath either tile.
	const sourceX = offsetX + source.x;
	const sourceY = offsetY + source.y + LINEAGE_TILE_HEIGHT / 2;
	const targetX = offsetX + target.x;
	const targetY = offsetY + target.y - LINEAGE_TILE_HEIGHT / 2;
	const middleY = (sourceY + targetY) / 2;
	return `M ${sourceX} ${sourceY} C ${sourceX} ${middleY}, ${targetX} ${middleY}, ${targetX} ${targetY}`;
}

export function lineageAncestorEdgeIds<T>(
	edges: PositionedLineageEdge<T>[],
	nodeId: string
): Set<string> {
	const parentEdgeByNode = new Map(edges.map((edge) => [edge.target.id, edge]));
	const ids = new Set<string>();
	let currentId = nodeId;
	while (parentEdgeByNode.has(currentId)) {
		const edge = parentEdgeByNode.get(currentId) as PositionedLineageEdge<T>;
		ids.add(edge.id);
		currentId = edge.source.id;
	}
	return ids;
}

export function viewportWorldRect(
	width: number,
	height: number,
	translateX: number,
	translateY: number,
	scale: number,
	padding = 240
): WorldRect {
	return {
		left: -translateX / scale - padding,
		top: -translateY / scale - padding,
		right: (width - translateX) / scale + padding,
		bottom: (height - translateY) / scale + padding
	};
}

export function rectsIntersect(left: WorldRect, right: WorldRect): boolean {
	return !(
		left.right < right.left ||
		left.left > right.right ||
		left.bottom < right.top ||
		left.top > right.bottom
	);
}
