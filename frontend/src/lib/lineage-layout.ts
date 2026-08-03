import { hierarchy, tree } from 'd3-hierarchy';

export const LINEAGE_TILE_WIDTH = 216;
export const LINEAGE_TILE_HEIGHT = 176;
const DEPTH_GAP = 96;
const SIBLING_GAP = 56;
const TREE_GAP = 192;
const ROW_GAP = 240;
const GRID_GAP = 48;

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
	const tidy = tree<LineageLayoutNode<T>>().nodeSize([
		LINEAGE_TILE_HEIGHT + SIBLING_GAP,
		LINEAGE_TILE_WIDTH + DEPTH_GAP
	]);
	tidy(rootHierarchy);

	const raw = rootHierarchy.descendants().map((node) => ({
		id: node.data.id,
		x: node.y ?? 0,
		y: node.x ?? 0,
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
	trees: { rootId: string; createdAt: string; layout: LineageTreeLayout<T> }[],
	shelfWidth = 2400
): PackedLineageTree<T>[] {
	const ordered = [...trees].sort(
		(left, right) =>
			right.createdAt.localeCompare(left.createdAt) || left.rootId.localeCompare(right.rootId)
	);
	const branched = ordered.filter((item) => item.layout.nodes.length > 1);
	const singles = ordered.filter((item) => item.layout.nodes.length === 1);
	const packed: PackedLineageTree<T>[] = [];
	let x = 0;
	let y = 0;
	let rowHeight = 0;

	for (const item of branched) {
		if (x > 0 && x + item.layout.width > shelfWidth) {
			x = 0;
			y += rowHeight + ROW_GAP;
			rowHeight = 0;
		}
		packed.push({ ...item, x, y });
		x += item.layout.width + TREE_GAP;
		rowHeight = Math.max(rowHeight, item.layout.height);
	}

	if (branched.length > 0) y += rowHeight + ROW_GAP;
	x = 0;
	const cellWidth = LINEAGE_TILE_WIDTH + GRID_GAP;
	const cellHeight = LINEAGE_TILE_HEIGHT + GRID_GAP;
	const columns = Math.max(1, Math.floor(shelfWidth / cellWidth));
	for (const [index, item] of singles.entries()) {
		packed.push({
			...item,
			x: (index % columns) * cellWidth,
			y: y + Math.floor(index / columns) * cellHeight
		});
	}
	return packed;
}

export function lineageLod(scale: number): 'constellation' | 'trees' | 'cards' {
	if (scale < 0.25) return 'constellation';
	if (scale < 0.8) return 'trees';
	return 'cards';
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
