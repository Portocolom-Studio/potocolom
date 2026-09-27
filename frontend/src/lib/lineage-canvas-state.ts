// One million CSS pixels leaves ample canvas room while bounding persisted transforms.
export const LINEAGE_WORLD_LIMIT = 1_000_000;

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

/** What the viewport should do after one root page loads or fails.
 *
 * `initialize` wins whether or not the page arrived: a failed page during the
 * anchor hunt must still reach the newest-tree fallback, or the canvas strands
 * uninitialised with nothing left to schedule it. A failed page on an account
 * with no roots at all is the exception, because centring nothing while still
 * marking the viewport ready would leave a successful retry no reason to run
 * initialization again; staying unready is what keeps that retry's
 * initialization on the table. `recenter` applies only to a page that actually
 * arrived, because recentring against content the canvas never received would
 * centre on stale roots. */
export function decideViewportScheduleAfterRootPage(
	pageLoaded: boolean,
	viewportReady: boolean,
	recenterPending: boolean,
	hasRoots: boolean
): 'initialize' | 'recenter' | 'nothing' {
	if (!viewportReady && (pageLoaded || hasRoots)) return 'initialize';
	if (pageLoaded && recenterPending) return 'recenter';
	return 'nothing';
}

export function shouldSpendAnchorSearchPage(args: {
	viewportReady: boolean;
	rootsHaveMore: boolean;
	rootsFailed: boolean;
	rootsLoading: boolean;
	pagesUsed: number;
	maxPages: number;
	missingAnchor: boolean;
}): boolean {
	// True only when initializeViewport would start a roots request. loadRoots
	// no-ops while a page is in flight, so spending the allowance then burns
	// one of four searches without a request (issue #303).
	return (
		!args.viewportReady &&
		args.missingAnchor &&
		args.rootsHaveMore &&
		!args.rootsFailed &&
		!args.rootsLoading &&
		args.pagesUsed < args.maxPages
	);
}

export type LineageRoverTree = {
	rootId: string;
	createdAt: string;
	nodes: { id: string; createdAt: string }[];
	edges: { parentId: string; childId: string }[];
};

export type LineageRover = {
	order: string[];
	parentOf: Record<string, string | undefined>;
	childrenOf: Record<string, string[]>;
	previousSibling: Record<string, string | undefined>;
	nextSibling: Record<string, string | undefined>;
};

function sortByRecencyThenId(
	left: { id: string; createdAt: string },
	right: { id: string; createdAt: string }
): number {
	return right.createdAt.localeCompare(left.createdAt) || left.id.localeCompare(right.id);
}

function linkSiblings(
	ids: string[],
	previousSibling: Record<string, string | undefined>,
	nextSibling: Record<string, string | undefined>
): void {
	for (const [index, id] of ids.entries()) {
		previousSibling[id] = ids[index - 1];
		nextSibling[id] = ids[index + 1];
	}
}

/** Roots newest first, then each tree in child-created order. Arrow keys walk
 * parent / first child / previous sibling / next sibling on that graph. */
export function buildLineageRover(trees: LineageRoverTree[]): LineageRover {
	const orderedTrees = [...trees].sort((left, right) =>
		sortByRecencyThenId(
			{ id: left.rootId, createdAt: left.createdAt },
			{ id: right.rootId, createdAt: right.createdAt }
		)
	);
	const parentOf: Record<string, string | undefined> = {};
	const childrenOf: Record<string, string[]> = {};
	const order: string[] = [];

	for (const tree of orderedTrees) {
		const createdAt = new Map(tree.nodes.map((node) => [node.id, node.createdAt]));
		const children = new Map<string, string[]>();
		for (const edge of tree.edges) {
			const list = children.get(edge.parentId) ?? [];
			list.push(edge.childId);
			children.set(edge.parentId, list);
			parentOf[edge.childId] = edge.parentId;
		}
		for (const [parentId, childIds] of children) {
			childIds.sort(
				(left, right) =>
					(createdAt.get(left) ?? '').localeCompare(createdAt.get(right) ?? '') ||
					left.localeCompare(right)
			);
			childrenOf[parentId] = childIds;
		}
		const visit = (id: string): void => {
			order.push(id);
			for (const childId of children.get(id) ?? []) visit(childId);
		};
		visit(tree.rootId);
	}

	const previousSibling: Record<string, string | undefined> = {};
	const nextSibling: Record<string, string | undefined> = {};
	linkSiblings(
		orderedTrees.map((tree) => tree.rootId),
		previousSibling,
		nextSibling
	);
	for (const childIds of Object.values(childrenOf)) {
		linkSiblings(childIds, previousSibling, nextSibling);
	}

	return { order, parentOf, childrenOf, previousSibling, nextSibling };
}

export function nextLineageRoverId(
	rover: LineageRover,
	currentId: string,
	key: string
): string | null {
	if (key === 'ArrowUp') return rover.parentOf[currentId] ?? null;
	if (key === 'ArrowDown') return rover.childrenOf[currentId]?.[0] ?? null;
	if (key === 'ArrowLeft') return rover.previousSibling[currentId] ?? null;
	if (key === 'ArrowRight') return rover.nextSibling[currentId] ?? null;
	return null;
}

export function lineageTileTabIndex(nodeId: string, focusedId: string | null): 0 | -1 {
	return focusedId === nodeId ? 0 : -1;
}

export function lineageRoverEntryId(rover: LineageRover): string | null {
	return rover.order[0] ?? null;
}

export type LineageFocusActive =
	'focused-tile' | 'viewport' | 'inspector' | 'other-tile' | 'other' | 'none';

export function lineageFocusActiveKind(args: {
	focusedId: string;
	activeIsNull: boolean;
	activeIsViewport: boolean;
	activeIsInspector: boolean;
	activeNodeId: string | null;
}): LineageFocusActive {
	if (args.activeIsNull) return 'none';
	if (args.activeIsViewport) return 'viewport';
	if (args.activeIsInspector) return 'inspector';
	if (args.activeNodeId === args.focusedId) return 'focused-tile';
	if (args.activeNodeId !== null) return 'other-tile';
	return 'other';
}

/** Restore a tile only after it unmounted (body) or the rover moved
 * (another tile). Restoring from the viewport would steal the arrows the
 * decision keeps for panning. */
export function shouldRestoreLineageTileFocus(
	focusedId: string | null,
	tileIsMounted: boolean,
	kind: LineageFocusActive
): boolean {
	if (focusedId === null || !tileIsMounted) return false;
	return kind === 'none' || kind === 'other-tile';
}

/** Visibility culling may omit a node; the focused id still has to mount so
 * the accessibility tree can name it after the rover moves off screen. */
export function lineageMountedIds(
	nodes: { id: string; inView: boolean }[],
	focusedId: string | null,
	maxMounted: number
): Set<string> {
	const mounted: string[] = [];
	for (const node of nodes) {
		if (!node.inView) continue;
		mounted.push(node.id);
		if (mounted.length === maxMounted) break;
	}
	if (
		focusedId !== null &&
		!mounted.includes(focusedId) &&
		nodes.some((node) => node.id === focusedId)
	) {
		if (mounted.length >= maxMounted) mounted.pop();
		mounted.push(focusedId);
	}
	return new Set(mounted);
}

export function shouldPanToFocusedNode(args: {
	nodeX: number;
	nodeY: number;
	tileWidth: number;
	tileHeight: number;
	viewportWidth: number;
	viewportHeight: number;
	translateX: number;
	translateY: number;
	scale: number;
}): boolean {
	const halfWidth = args.tileWidth / 2;
	const halfHeight = args.tileHeight / 2;
	const left = args.nodeX - halfWidth;
	const right = args.nodeX + halfWidth;
	const top = args.nodeY - halfHeight;
	const bottom = args.nodeY + halfHeight;
	const viewLeft = -args.translateX / args.scale;
	const viewTop = -args.translateY / args.scale;
	const viewRight = (args.viewportWidth - args.translateX) / args.scale;
	const viewBottom = (args.viewportHeight - args.translateY) / args.scale;
	return left < viewLeft || right > viewRight || top < viewTop || bottom > viewBottom;
}

export function translationToCenterNode(
	nodeX: number,
	nodeY: number,
	viewportWidth: number,
	viewportHeight: number,
	scale: number
): { translateX: number; translateY: number } {
	return {
		translateX: clampLineageCoordinate(viewportWidth / 2 - nodeX * scale),
		translateY: clampLineageCoordinate(viewportHeight / 2 - nodeY * scale)
	};
}
