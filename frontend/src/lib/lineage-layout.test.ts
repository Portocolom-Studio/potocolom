import assert from 'node:assert/strict';
import test from 'node:test';
import {
	LINEAGE_TILE_HEIGHT,
	LINEAGE_TILE_WIDTH,
	layoutLineageTree,
	lineageAncestorEdgeIds,
	lineageEdgePath,
	lineageLod,
	packLineageForest,
	type LineageLayoutNode
} from './lineage-layout.ts';

type TestData = { label: string };

function node(
	id: string,
	createdAt: string,
	children: LineageLayoutNode<TestData>[] = []
): LineageLayoutNode<TestData> {
	return { id, createdAt, data: { label: id }, children };
}

test('tidy layout is deterministic from ids and creation ordering', () => {
	const first = node('root', '2026-01-01T00:00:00Z', [
		node('later', '2026-01-03T00:00:00Z'),
		node('earlier', '2026-01-02T00:00:00Z')
	]);
	const second = node('root', '2026-01-01T00:00:00Z', [
		node('earlier', '2026-01-02T00:00:00Z'),
		node('later', '2026-01-03T00:00:00Z')
	]);

	assert.deepEqual(layoutLineageTree(first), layoutLineageTree(second));
});

test('LOD changes content band without changing node coordinates', () => {
	const layout = layoutLineageTree(
		node('root', '2026-01-01T00:00:00Z', [node('child', '2026-01-02T00:00:00Z')])
	);
	const coordinates = layout.nodes.map(({ id, x, y }) => ({ id, x, y }));

	assert.equal(lineageLod(0.2), 'constellation');
	assert.equal(lineageLod(0.5), 'trees');
	assert.equal(lineageLod(1), 'cards');
	assert.deepEqual(
		layout.nodes.map(({ id, x, y }) => ({ id, x, y })),
		coordinates
	);
});

test('edges meet tile borders and ancestry follows parents to the root', () => {
	const layout = layoutLineageTree(
		node('root', '2026-01-01T00:00:00Z', [
			node('child', '2026-01-02T00:00:00Z', [node('leaf', '2026-01-03T00:00:00Z')]),
			node('sibling', '2026-01-04T00:00:00Z')
		])
	);
	const rootEdge = layout.edges.find((edge) => edge.target.id === 'child');
	assert.ok(rootEdge);

	// Trees grow downward, so an edge leaves the parent's bottom and meets the
	// child's top; the child also sits strictly below its parent.
	const sourceY = rootEdge.source.y + LINEAGE_TILE_HEIGHT / 2;
	const targetY = rootEdge.target.y - LINEAGE_TILE_HEIGHT / 2;
	const path = lineageEdgePath(rootEdge.source, rootEdge.target);
	assert.match(path, new RegExp(`^M ${rootEdge.source.x} ${sourceY} `));
	assert.match(path, new RegExp(` ${targetY}$`));
	assert.ok(rootEdge.target.y > rootEdge.source.y);
	assert.equal(rootEdge.target.y - rootEdge.source.y, LINEAGE_TILE_HEIGHT + 96);
	assert.deepEqual([...lineageAncestorEdgeIds(layout.edges, 'leaf')], ['child:leaf', 'root:child']);
});

test('forest packing puts branched trees first and single roots in a grid', () => {
	const newestSingle = layoutLineageTree(node('newest', '2026-01-03T00:00:00Z'));
	const olderTree = layoutLineageTree(
		node('tree', '2026-01-02T00:00:00Z', [node('child', '2026-01-03T00:00:00Z')])
	);
	const oldestSingle = layoutLineageTree(node('oldest', '2026-01-01T00:00:00Z'));
	const packed = packLineageForest([
		{
			rootId: 'oldest',
			createdAt: '2026-01-01T00:00:00Z',
			hasDerivatives: false,
			layout: oldestSingle
		},
		{
			rootId: 'tree',
			createdAt: '2026-01-02T00:00:00Z',
			hasDerivatives: true,
			layout: olderTree
		},
		{
			rootId: 'newest',
			createdAt: '2026-01-03T00:00:00Z',
			hasDerivatives: false,
			layout: newestSingle
		}
	]);

	assert.equal(packed[0].rootId, 'tree');
	assert.equal(packed[1].rootId, 'newest');
	assert.equal(packed[2].rootId, 'oldest');
	assert.equal(packed[2].x, packed[1].x);
	assert.equal(packed[2].y - packed[1].y, LINEAGE_TILE_HEIGHT + 48);
	assert.ok(packed[0].y > packed[2].y);
	assert.ok(oldestSingle.height >= LINEAGE_TILE_HEIGHT);
});

test('forest packing is stable while a declared tree loads', () => {
	const rootOnly = layoutLineageTree(node('tree', '2026-01-03T00:00:00Z'));
	const loadedTree = layoutLineageTree(
		node('tree', '2026-01-03T00:00:00Z', [
			node('left', '2026-01-04T00:00:00Z'),
			node('right', '2026-01-05T00:00:00Z')
		])
	);
	const single = layoutLineageTree(node('single', '2026-01-02T00:00:00Z'));
	const pack = (layout: typeof rootOnly) =>
		packLineageForest([
			{
				rootId: 'tree',
				createdAt: '2026-01-03T00:00:00Z',
				hasDerivatives: true,
				layout
			},
			{
				rootId: 'single',
				createdAt: '2026-01-02T00:00:00Z',
				hasDerivatives: false,
				layout: single
			}
		]);
	const loading = pack(rootOnly);
	const loaded = pack(loadedTree);
	const rootPosition = (trees: typeof loading) => {
		const tree = trees.find((item) => item.rootId === 'tree');
		const root = tree?.layout.nodes.find((item) => item.id === 'tree');
		return { x: (tree?.x ?? 0) + (root?.x ?? 0), y: (tree?.y ?? 0) + (root?.y ?? 0) };
	};

	assert.deepEqual(rootPosition(loading), rootPosition(loaded));
	assert.deepEqual(
		loading.filter((item) => item.rootId === 'single').map(({ x, y }) => ({ x, y })),
		loaded.filter((item) => item.rootId === 'single').map(({ x, y }) => ({ x, y }))
	);
});
