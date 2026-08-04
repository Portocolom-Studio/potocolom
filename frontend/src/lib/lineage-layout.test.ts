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
		{ rootId: 'oldest', createdAt: '2026-01-01T00:00:00Z', layout: oldestSingle },
		{ rootId: 'tree', createdAt: '2026-01-02T00:00:00Z', layout: olderTree },
		{ rootId: 'newest', createdAt: '2026-01-03T00:00:00Z', layout: newestSingle }
	]);

	assert.equal(packed[0].rootId, 'tree');
	assert.equal(packed[1].rootId, 'newest');
	assert.equal(packed[2].rootId, 'oldest');
	assert.equal(packed[2].x - packed[1].x, LINEAGE_TILE_WIDTH + 48);
	assert.ok(packed[1].y >= olderTree.height + 240);
	assert.ok(oldestSingle.height >= LINEAGE_TILE_HEIGHT);
});
