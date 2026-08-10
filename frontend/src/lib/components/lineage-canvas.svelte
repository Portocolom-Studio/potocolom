<script module lang="ts">
	import type { LineageTreeLayout } from '$lib/lineage-layout';
	import type {
		Generation as CanvasGeneration,
		LineageEntry as CanvasLineageEntry
	} from '$lib/studio.svelte';

	type CanvasNodeData = {
		output_asset_ids: string[];
		entry: CanvasLineageEntry;
		generation: CanvasGeneration | null;
	};

	type CachedTree = {
		status: 'loading' | 'loaded' | 'error';
		layout: LineageTreeLayout<CanvasNodeData> | null;
		dirty: boolean;
		truncated: boolean;
		omittedHistoryJobIds: ReadonlySet<string>;
		remainingCountLowerBound: number;
		// One automatic retry per failure, tracked on the entry rather than per
		// root: every load replaces the entry, so a later failure gets its own
		// retry instead of inheriting an exhausted budget from an earlier one.
		// Required, not optional: dropping it from a transition is what made the
		// retry loop forever, so every entry has to state its budget out loud.
		retried: boolean | undefined;
	};

	const sessionTreeCache = new Map<string, CachedTree>();
	let canvasEpochSequence = 0;
</script>

<script lang="ts">
	import { onDestroy, onMount, tick, untrack } from 'svelte';
	import DownloadIcon from '@lucide/svelte/icons/download';
	import ImageOffIcon from '@lucide/svelte/icons/image-off';
	import LoaderCircleIcon from '@lucide/svelte/icons/loader-circle';
	import LocateFixedIcon from '@lucide/svelte/icons/locate-fixed';
	import MinusIcon from '@lucide/svelte/icons/minus';
	import MoveIcon from '@lucide/svelte/icons/move';
	import PencilIcon from '@lucide/svelte/icons/pencil';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import RotateCcwIcon from '@lucide/svelte/icons/rotate-ccw';
	import ScanLineIcon from '@lucide/svelte/icons/scan-line';
	import StarIcon from '@lucide/svelte/icons/star';
	import WandSparklesIcon from '@lucide/svelte/icons/wand-sparkles';
	import XIcon from '@lucide/svelte/icons/x';
	import { Button } from '$lib/components/ui/button';
	import { getLocale, t } from '$lib/i18n.svelte';
	import {
		clampLineageCoordinate,
		decideInitialLineageViewportFollow,
		decideLineageLiveArrival,
		decideLineageTreeLoad,
		lineageRootPageUrl,
		lineageTreeOmittedHistoryJobIds,
		lineageTreeNeedsHistoryRefresh,
		rebaseLineageViewport,
		retainedLineageTreeOffsets,
		retainedRetryBudget,
		shouldDimLineageEdge,
		shouldReloadLineageRootsAfterStarToggle,
		type InitialLineageViewportAnchor
	} from '$lib/lineage-canvas-state';
	import {
		LINEAGE_TILE_HEIGHT,
		LINEAGE_TILE_WIDTH,
		layoutLineageTree,
		lineageAncestorEdgeIds,
		lineageEdgePath,
		lineageLod,
		packLineageForest,
		rectsIntersect,
		viewportWorldRect,
		type LineageLayoutNode,
		type PackedLineageTree,
		type PositionedLineageNode
	} from '$lib/lineage-layout';
	import {
		defaultUpscaleModelId,
		filterImageToImageModels,
		filterTextToImageModels,
		filterUpscaleModels,
		generationById,
		isStarred,
		openService,
		saveLineageTreeOffsets,
		saveLineageViewport,
		selectGeneration,
		studio,
		toggleStarred,
		type Generation,
		type GenerationSubtree,
		type LineageEntry
	} from '$lib/studio.svelte';

	const ROOT_LIMIT = 50;
	// A saved viewport names the root it was anchored to, and that root may sit
	// several pages back. Hunting it costs one round trip per page with nothing
	// drawn yet, and it never resolves at all when the anchor has since expired,
	// so give up after this many and open on the newest instead.
	const MAX_ANCHOR_SEARCH_PAGES = 4;
	const MAX_MOUNTED_TILES = 600;
	const MAX_CONCURRENT_TREE_LOADS = 4;
	const MIN_SCALE = 0.12;
	const MAX_SCALE = 1.6;
	const PAN_STEP = 80;
	const TREE_KEYBOARD_STEP = 24;
	const VIEWPORT_SAVE_DELAY = 300;
	const HOVER_RADIUS = 150;
	const HOVER_PULL = 0.08;
	const INERTIA_MIN = 0.04;
	const INERTIA_FRICTION = 0.004;

	type VisibleNode = {
		rootId: string;
		x: number;
		y: number;
		isRoot: boolean;
		treeStatus: CachedTree['status'] | null;
		remainingCountLowerBound: number;
		node: PositionedLineageNode<CanvasNodeData>;
	};

	type PointerSample = { x: number; y: number; time: number };
	const restoredViewport = studio.lineageViewport;

	let viewportEl = $state<HTMLDivElement | null>(null);
	let inspectorEl = $state<HTMLElement | null>(null);
	let viewportWidth = $state(0);
	let viewportHeight = $state(0);
	let translateX = $state(restoredViewport?.translateX ?? 72);
	let translateY = $state(restoredViewport?.translateY ?? 72);
	let scale = $state(restoredViewport?.scale ?? 1);
	let treeOffsets = $state({ ...studio.lineageTreeOffsets });
	let roots = $state<Generation[]>([]);
	let rootsLoading = $state(false);
	let rootsInitialized = $state(false);
	let rootsFailed = $state(false);
	let rootsHaveMore = $state(false);
	let starredOnly = $state(false);
	let rootsFilterEpoch = 0;
	let recenterAfterFilter = false;
	let anchorSearchPages = 0;
	let initializeFrame = 0;
	let treeCache = $state(new Map(sessionTreeCache));
	let newNodeIds = $state(new Set<string>());
	let failedImageIds = $state(new Set<string>());
	let refreshingImageIds = $state(new Set<string>());
	let refreshedImageIds = new Set<string>();
	let pointerWorld = $state<{ x: number; y: number } | null>(null);
	let focusedNodeId = $state<string | null>(null);
	let selectionOrigin: HTMLButtonElement | null = null;
	let reducedMotion = false;
	let recentering = $state(false);
	let recenterTimer: ReturnType<typeof setTimeout> | null = null;
	let viewportSaveTimer: ReturnType<typeof setTimeout> | null = null;
	let viewportReady = $state(false);
	let initialViewportAnchor: InitialLineageViewportAnchor | null = null;
	let inertiaFrame = 0;
	let panPointerId = $state<number | null>(null);
	let panStart = { x: 0, y: 0, translateX: 0, translateY: 0 };
	let lastPanSample: PointerSample | null = null;
	let panVelocity = { x: 0, y: 0 };
	let pinch = $state<{ distance: number; worldX: number; worldY: number } | null>(null);
	let dragPointerId = $state<number | null>(null);
	let draggedRootId = $state<string | null>(null);
	let dragMoved = false;
	let suppressTreeClick: string | null = null;
	let dragStart = { x: 0, y: 0, offsetX: 0, offsetY: 0 };
	let lastPageLoadWorld = { right: Number.NEGATIVE_INFINITY, bottom: Number.NEGATIVE_INFINITY };
	const pointers = new Map<number, { x: number; y: number }>();
	const canvasEpoch = ++canvasEpochSequence;
	let canvasActive = true;
	const requestControllers = new Set<AbortController>();
	const treeLoadQueue = new Map<string, { root: Generation; force: boolean }>();
	let treeLoadsInFlight = 0;
	const knownFinishedIds = new Set(
		studio.history
			.filter((generation) => generation.assets.length > 0)
			.map((generation) => generation.id)
	);

	const persistedRoots = $derived(roots.filter((root) => root.assets.length > 0));
	const lod = $derived(lineageLod(scale));
	const worldRect = $derived(
		viewportWorldRect(viewportWidth, viewportHeight, translateX, translateY, scale)
	);
	const basePackedTrees = $derived.by(() => {
		const layouts = persistedRoots.map((root) => {
			const cached = treeCache.get(root.id)?.layout;
			return {
				rootId: root.id,
				createdAt: root.created_at,
				hasDerivatives: root.has_derivatives === true,
				layout: cached ?? layoutLineageTree(rootLayoutNode(root))
			};
		});
		return packLineageForest(layouts);
	});
	const packedTrees = $derived(
		basePackedTrees.map((tree) => ({
			...tree,
			x: tree.x + (treeOffsets[tree.rootId]?.x ?? 0),
			y: tree.y + (treeOffsets[tree.rootId]?.y ?? 0)
		}))
	);
	const hasTreeOffsets = $derived(Object.keys(treeOffsets).length > 0);
	const visibleNodes = $derived.by(() => {
		const shown: VisibleNode[] = [];
		for (const packed of packedTrees) {
			for (const node of packed.layout.nodes) {
				const x = packed.x + node.x;
				const y = packed.y + node.y;
				if (
					rectsIntersect(worldRect, {
						left: x - LINEAGE_TILE_WIDTH / 2,
						top: y - LINEAGE_TILE_HEIGHT / 2,
						right: x + LINEAGE_TILE_WIDTH / 2,
						bottom: y + LINEAGE_TILE_HEIGHT / 2
					})
				) {
					shown.push({
						rootId: packed.rootId,
						x,
						y,
						isRoot: node.id === packed.layout.rootId,
						treeStatus: treeCache.get(packed.rootId)?.status ?? null,
						remainingCountLowerBound: treeCache.get(packed.rootId)?.remainingCountLowerBound ?? 0,
						node
					});
					if (shown.length === MAX_MOUNTED_TILES) return shown;
				}
			}
		}
		return shown;
	});
	const forestBottom = $derived(
		Math.max(0, ...packedTrees.map((packed) => packed.y + packed.layout.height))
	);
	const forestRight = $derived(
		Math.max(0, ...packedTrees.map((packed) => packed.x + packed.layout.width))
	);
	const selectedNode = $derived.by(() => {
		if (studio.lineageSelectedAssetId === null) return null;
		for (const tree of packedTrees) {
			const node = tree.layout.nodes.find((item) => item.id === studio.lineageSelectedAssetId);
			if (node) return node;
		}
		return null;
	});
	const selectedData = $derived(selectedNode?.data ?? null);
	const selectedGeneration = $derived(
		selectedData?.generation ??
			(selectedData?.entry.job_id ? (generationById(selectedData.entry.job_id) ?? null) : null)
	);
	const selectedIsStarred = $derived(
		selectedGeneration !== null && isStarred(selectedGeneration.id)
	);
	const selectedAsset = $derived(
		selectedGeneration?.assets.find((asset) => asset.id === studio.lineageSelectedAssetId) ?? null
	);
	const selectedImageUrl = $derived(
		selectedData !== null && !selectedData.entry.missing ? (selectedAsset?.url ?? null) : null
	);
	const selectedPrompt = $derived((selectedGeneration?.params.prompt ?? '').trim());
	const selectedParams = $derived(
		Object.entries(selectedGeneration?.params ?? {}).filter(([key]) => key !== 'prompt')
	);
	const textToImageModels = $derived(filterTextToImageModels(studio.models));
	const imageToImageModels = $derived(filterImageToImageModels(studio.models));
	const upscaleModels = $derived(filterUpscaleModels(studio.models));
	const selectedHasBytes = $derived(
		selectedData !== null && !selectedData.entry.missing && selectedAsset !== null
	);
	const canGenerateFromPrompt = $derived(selectedPrompt !== '' && textToImageModels.length > 0);
	const canEditSelected = $derived(selectedHasBytes && imageToImageModels.length > 0);
	const canUpscaleSelected = $derived(selectedHasBytes && upscaleModels.length > 0);
	const selectedPathEdgeIds = $derived.by(() => {
		if (studio.lineageSelectedAssetId === null) return new Set<string>();
		const tree = packedTrees.find((item) =>
			item.layout.nodes.some((node) => node.id === studio.lineageSelectedAssetId)
		);
		return tree
			? lineageAncestorEdgeIds(tree.layout.edges, studio.lineageSelectedAssetId)
			: new Set<string>();
	});

	function rootLayoutNode(root: Generation): LineageLayoutNode<CanvasNodeData> {
		const asset = root.assets[0];
		return {
			id: asset.id,
			createdAt: root.created_at,
			data: {
				output_asset_ids: root.assets.map((item) => item.id),
				entry: {
					job_id: root.id,
					asset_id: asset.id,
					action: 'generate',
					model_id: root.model_id,
					created_at: root.created_at,
					state: root.state,
					thumbnail_url: asset.thumbnail_url,
					missing: false
				},
				generation: root
			},
			children: []
		};
	}

	function setCachedTree(rootId: string, tree: CachedTree): void {
		if (!canvasActive || canvasEpoch !== canvasEpochSequence) return;
		const next = new Map(treeCache);
		next.set(rootId, tree);
		treeCache = next;
		if (tree.status === 'loaded') sessionTreeCache.set(rootId, tree);
	}

	async function fetchCanvasJson<T>(url: string, failure: string): Promise<T> {
		const controller = new AbortController();
		requestControllers.add(controller);
		try {
			const response = await fetch(url, { signal: controller.signal });
			if (!response.ok) throw new Error(failure);
			const value = (await response.json()) as T;
			if (!canvasActive || canvasEpoch !== canvasEpochSequence) {
				throw new DOMException('stale canvas request', 'AbortError');
			}
			return value;
		} finally {
			requestControllers.delete(controller);
		}
	}

	async function loadRoots(): Promise<void> {
		if (rootsLoading || (!rootsHaveMore && roots.length > 0)) return;
		const filterEpoch = rootsFilterEpoch;
		const filterStarredOnly = starredOnly;
		rootsLoading = true;
		rootsFailed = false;
		const cursor = roots.at(-1)?.id;
		let loaded = false;
		try {
			// Starred mode selects starred roots, then keeps each returned root's
			// complete subtree. Filtering descendants individually would sever the
			// provenance that makes this a forest rather than a list of cards.
			const page = await fetchCanvasJson<Generation[]>(
				lineageRootPageUrl(ROOT_LIMIT, cursor ?? null, filterStarredOnly),
				'history request failed'
			);
			if (filterEpoch !== rootsFilterEpoch) return;
			const existing = new Set(roots.map((root) => root.id));
			roots = [...roots, ...page.filter((root) => !existing.has(root.id))];
			rootsHaveMore = page.length === ROOT_LIMIT;
			rootsInitialized = true;
			loaded = true;
		} catch (error) {
			if (error instanceof DOMException && error.name === 'AbortError') return;
			if (filterEpoch === rootsFilterEpoch) rootsFailed = true;
		} finally {
			if (canvasActive && canvasEpoch === canvasEpochSequence && filterEpoch === rootsFilterEpoch) {
				rootsLoading = false;
			}
		}
		if (loaded && !viewportReady) {
			initializeFrame = requestAnimationFrame(initializeViewport);
		} else if (loaded && recenterAfterFilter) {
			recenterAfterFilter = false;
			initializeFrame = requestAnimationFrame(() => {
				initialViewportAnchor = recenterNewest(false);
			});
		}
	}

	function reloadRootsForFilter(value: boolean): void {
		rootsFilterEpoch += 1;
		starredOnly = value;
		roots = [];
		rootsLoading = false;
		rootsInitialized = false;
		rootsFailed = false;
		rootsHaveMore = false;
		recenterAfterFilter = viewportReady;
		lastPageLoadWorld = { right: Number.NEGATIVE_INFINITY, bottom: Number.NEGATIVE_INFINITY };
		void loadRoots();
	}

	function setStarredOnly(value: boolean): void {
		if (starredOnly === value) return;
		reloadRootsForFilter(value);
	}

	async function toggleSelectedStar(): Promise<void> {
		if (selectedGeneration === null) return;
		const filterEpoch = rootsFilterEpoch;
		const selectedWasRoot = roots.some((root) => root.id === selectedGeneration.id);
		const filteredRootChanged = starredOnly && selectedWasRoot;
		const succeeded = await toggleStarred(selectedGeneration.id);
		if (
			shouldReloadLineageRootsAfterStarToggle(
				succeeded,
				filteredRootChanged,
				filterEpoch,
				rootsFilterEpoch,
				starredOnly
			)
		) {
			reloadRootsForFilter(true);
		}
	}

	// Arrival markers have to come back off again. Tiles unmount once they leave
	// the viewport, so an id left in the set replays its entry animation every
	// time that tile is panned back into view.
	function markArrived(ids: string[]): void {
		if (reducedMotion || ids.length === 0) return;
		newNodeIds = new Set([...newNodeIds, ...ids]);
		setTimeout(() => {
			newNodeIds = new Set([...newNodeIds].filter((id) => !ids.includes(id)));
		}, 240);
	}

	async function loadTree(root: Generation, force = false): Promise<void> {
		const existing = treeCache.get(root.id);
		if (existing?.status === 'loading') {
			if (force && !existing.dirty) setCachedTree(root.id, { ...existing, dirty: true });
			return;
		}
		if (existing?.status === 'loaded' && !force) return;
		const retained = retainedRetryBudget(force, existing);
		setCachedTree(root.id, {
			status: 'loading',
			layout: existing?.layout ?? null,
			dirty: false,
			truncated: existing?.truncated ?? false,
			omittedHistoryJobIds: existing?.omittedHistoryJobIds ?? new Set(),
			remainingCountLowerBound: existing?.remainingCountLowerBound ?? 0,
			retried: retained
		});
		try {
			const subtree = await fetchCanvasJson<GenerationSubtree>(
				`/api/v1/generations/${root.id}/subtree`,
				'root subtree request failed'
			);
			const nodesByAsset = new Map<string, LineageLayoutNode<CanvasNodeData>>();
			const nodesByJob = new Map<string, LineageLayoutNode<CanvasNodeData>>();
			for (const node of subtree.nodes) {
				const layoutNode = {
					id: node.entry.asset_id,
					createdAt: node.entry.created_at,
					data: node,
					children: []
				};
				nodesByAsset.set(node.entry.asset_id, layoutNode);
				if (node.entry.job_id !== null) nodesByJob.set(node.entry.job_id, layoutNode);
			}
			for (const node of subtree.nodes) {
				if (node.parent_job_id === null) continue;
				const parent = nodesByJob.get(node.parent_job_id);
				const child = nodesByAsset.get(node.entry.asset_id);
				if (parent && child && parent !== child) parent.children.push(child);
			}
			const responseRoot = subtree.nodes.find((node) => node.entry.job_id === root.id);
			if (!responseRoot) throw new Error('root missing from subtree');
			const rootNode = nodesByAsset.get(responseRoot.entry.asset_id);
			if (!rootNode) throw new Error('root missing from subtree');
			const layout = layoutLineageTree(rootNode);
			const previousIds = new Set(existing?.layout?.nodes.map((node) => node.id) ?? []);
			const added = layout.nodes.map((node) => node.id).filter((id) => !previousIds.has(id));
			const rerun = treeCache.get(root.id)?.dirty === true;
			setCachedTree(root.id, {
				status: 'loaded',
				layout,
				dirty: false,
				truncated: subtree.truncated,
				omittedHistoryJobIds: subtree.truncated
					? lineageTreeOmittedHistoryJobIds(layout.nodes, studio.history)
					: new Set(),
				remainingCountLowerBound: subtree.remaining_count_lower_bound,
				// A load that worked owes nothing, so the next failure starts fresh.
				retried: undefined
			});
			roots = roots.map((item) => (item.id === root.id ? { ...responseRoot.generation } : item));
			if (previousIds.size > 0) markArrived(added);
			if (rerun) scheduleTreeLoad(responseRoot.generation, true);
		} catch (error) {
			if (error instanceof DOMException && error.name === 'AbortError') return;
			const rerun = treeCache.get(root.id)?.dirty === true;
			setCachedTree(root.id, {
				status: 'error',
				layout: existing?.layout ?? null,
				dirty: false,
				truncated: existing?.truncated ?? false,
				omittedHistoryJobIds: existing?.omittedHistoryJobIds ?? new Set(),
				remainingCountLowerBound: existing?.remainingCountLowerBound ?? 0,
				retried: retained
			});
			// A coalesced force is a fresh request rather than this failure's
			// automatic retry, so it starts with a budget of its own.
			if (rerun) scheduleTreeLoad(root, true);
		}
	}

	function scheduleTreeLoad(root: Generation, force = false): void {
		const cached = treeCache.get(root.id);
		if (cached?.status === 'loading') {
			if (force && !cached.dirty) setCachedTree(root.id, { ...cached, dirty: true });
			return;
		}
		const queued = treeLoadQueue.get(root.id);
		if (queued) {
			if (force && !queued.force) treeLoadQueue.set(root.id, { root, force: true });
			return;
		}
		treeLoadQueue.set(root.id, { root, force });
		drainTreeLoadQueue();
	}

	function drainTreeLoadQueue(): void {
		while (canvasActive && treeLoadsInFlight < MAX_CONCURRENT_TREE_LOADS) {
			const next = treeLoadQueue.entries().next().value as
				[string, { root: Generation; force: boolean }] | undefined;
			if (!next) return;
			const [rootId, request] = next;
			treeLoadQueue.delete(rootId);
			treeLoadsInFlight += 1;
			void loadTree(request.root, request.force).finally(() => {
				treeLoadsInFlight -= 1;
				drainTreeLoadQueue();
			});
		}
	}

	function treeIsVisible(tree: PackedLineageTree<CanvasNodeData>): boolean {
		return rectsIntersect(worldRect, {
			left: tree.x,
			top: tree.y,
			right: tree.x + tree.layout.width,
			bottom: tree.y + tree.layout.height
		});
	}

	$effect(() => {
		for (const tree of packedTrees) {
			if (!treeIsVisible(tree)) continue;
			const root = persistedRoots.find((item) => item.id === tree.rootId);
			if (!root) continue;
			const cached = treeCache.get(tree.rootId);
			const decision = decideLineageTreeLoad(tree.hasDerivatives, cached);
			if (decision === 'skip') continue;
			if (decision === 'synthesize') {
				setCachedTree(tree.rootId, {
					status: 'loaded',
					layout: tree.layout,
					dirty: false,
					truncated: false,
					omittedHistoryJobIds: new Set(),
					remainingCountLowerBound: 0,
					// Nothing was fetched, so there is no failure to budget for.
					retried: undefined
				});
				continue;
			}
			if (decision === 'retry' && cached) {
				setCachedTree(tree.rootId, { ...cached, retried: true });
			}
			scheduleTreeLoad(root);
		}
		const reachedRight =
			worldRect.right >= forestRight - 320 && worldRect.right >= lastPageLoadWorld.right + 320;
		const reachedBottom =
			worldRect.bottom >= forestBottom - 320 && worldRect.bottom >= lastPageLoadWorld.bottom + 320;
		if (rootsHaveMore && (reachedRight || reachedBottom)) {
			lastPageLoadWorld = { right: worldRect.right, bottom: worldRect.bottom };
			void loadRoots();
		}
	});

	$effect(() => {
		const finished = studio.history.filter((generation) => generation.assets.length > 0);
		for (const generation of finished) {
			if (knownFinishedIds.has(generation.id)) continue;
			knownFinishedIds.add(generation.id);
			const arrival = decideLineageLiveArrival(
				generation.source_asset_id === null,
				starredOnly,
				isStarred(generation.id)
			);
			if (arrival === 'ignore') continue;
			if (arrival === 'insert-root') {
				roots = [
					{ ...generation, has_derivatives: generation.has_derivatives ?? false },
					...roots.filter((root) => root.id !== generation.id)
				];
				markArrived([generation.assets[0].id]);
				continue;
			}
			for (const [rootId, cached] of treeCache) {
				if (!cached.layout?.nodes.some((node) => node.id === generation.source_asset_id)) continue;
				roots = roots.map((root) =>
					root.id === rootId ? { ...root, has_derivatives: true } : root
				);
				const root = roots.find((item) => item.id === rootId);
				if (root) scheduleTreeLoad(root, true);
				break;
			}
		}
	});

	$effect(() => {
		for (const root of persistedRoots) {
			const cached = treeCache.get(root.id);
			const cachedRoot = cached?.layout?.nodes.find((node) => node.data.entry.job_id === root.id);
			const derivativeFlagChanged =
				root.has_derivatives === true && cachedRoot?.data.generation?.has_derivatives !== true;
			if (
				cached?.status === 'loaded' &&
				cached.layout &&
				(derivativeFlagChanged ||
					lineageTreeNeedsHistoryRefresh(
						cached.layout.nodes,
						studio.history,
						cached.omittedHistoryJobIds
					))
			) {
				scheduleTreeLoad(root, true);
			}
		}
	});

	$effect(() => {
		if (!rootsInitialized || rootsLoading || rootsHaveMore) return;
		const rootIds = new Set(persistedRoots.map((root) => root.id));
		const bounded = retainedLineageTreeOffsets(treeOffsets, rootIds, starredOnly);
		if (Object.keys(bounded).length !== Object.keys(treeOffsets).length) {
			treeOffsets = bounded;
			saveLineageTreeOffsets(bounded);
		}
	});

	$effect(() => {
		if (!viewportReady || initialViewportAnchor === null) return;
		const storedAnchor = initialViewportAnchor;
		const current = rootWorldPosition(storedAnchor.rootId);
		// The viewport translation is input to the pure decision, but not a reason
		// to rerun this effect after it writes the decision back.
		const viewport = untrack(() => ({ translateX, translateY, scale }));
		const decision = decideInitialLineageViewportFollow(
			viewport,
			storedAnchor,
			current,
			rootsFailed ? 'failed' : rootsLoading ? 'loading' : 'settled'
		);
		if (decision.fallbackToNewest) {
			initialViewportAnchor = null;
			recenterNewest(false);
			return;
		}
		if (viewport.translateX !== decision.translateX) translateX = decision.translateX;
		if (viewport.translateY !== decision.translateY) translateY = decision.translateY;
		if (initialViewportAnchor !== decision.anchor) initialViewportAnchor = decision.anchor;
	});

	$effect(() => {
		const viewport = lineageViewport();
		if (!viewportReady) return;
		if (viewportSaveTimer) clearTimeout(viewportSaveTimer);
		viewportSaveTimer = setTimeout(() => saveLineageViewport(viewport), VIEWPORT_SAVE_DELAY);
		return () => {
			if (viewportSaveTimer) clearTimeout(viewportSaveTimer);
		};
	});

	function clampScale(value: number): number {
		return Math.min(MAX_SCALE, Math.max(MIN_SCALE, value));
	}

	function stopInertia(): void {
		if (inertiaFrame !== 0) cancelAnimationFrame(inertiaFrame);
		inertiaFrame = 0;
	}

	function startInertia(): void {
		if (reducedMotion || Math.hypot(panVelocity.x, panVelocity.y) < INERTIA_MIN) return;
		let previous = performance.now();
		const step = (now: number) => {
			const elapsed = Math.min(32, now - previous);
			previous = now;
			const friction = Math.exp(-INERTIA_FRICTION * elapsed);
			panVelocity.x *= friction;
			panVelocity.y *= friction;
			if (Math.hypot(panVelocity.x, panVelocity.y) < INERTIA_MIN) {
				inertiaFrame = 0;
				return;
			}
			translateX = clampLineageCoordinate(translateX + panVelocity.x * elapsed);
			translateY = clampLineageCoordinate(translateY + panVelocity.y * elapsed);
			inertiaFrame = requestAnimationFrame(step);
		};
		inertiaFrame = requestAnimationFrame(step);
	}

	function zoomAt(nextScale: number, cursorX: number, cursorY: number): void {
		const clamped = clampScale(nextScale);
		const worldX = (cursorX - translateX) / scale;
		const worldY = (cursorY - translateY) / scale;
		translateX = clampLineageCoordinate(cursorX - worldX * clamped);
		translateY = clampLineageCoordinate(cursorY - worldY * clamped);
		scale = clamped;
	}

	function onWheel(event: WheelEvent): void {
		event.preventDefault();
		stopInertia();
		const rect = viewportEl?.getBoundingClientRect();
		if (!rect) return;
		zoomAt(
			scale * Math.exp(-event.deltaY * 0.0015),
			event.clientX - rect.left,
			event.clientY - rect.top
		);
	}

	function onPointerDown(event: PointerEvent): void {
		if (event.button !== 0 || !viewportEl) return;
		if (event.target instanceof Element && event.target.closest('button')) return;
		stopInertia();
		pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
		if (pointers.size === 2) {
			const [first, second] = [...pointers.values()];
			const rect = viewportEl.getBoundingClientRect();
			const midpointX = (first.x + second.x) / 2 - rect.left;
			const midpointY = (first.y + second.y) / 2 - rect.top;
			pinch = {
				distance: Math.hypot(second.x - first.x, second.y - first.y),
				worldX: (midpointX - translateX) / scale,
				worldY: (midpointY - translateY) / scale
			};
			panPointerId = null;
			viewportEl.setPointerCapture(event.pointerId);
			return;
		}
		viewportEl.setPointerCapture(event.pointerId);
		panPointerId = event.pointerId;
		panStart = {
			x: event.clientX,
			y: event.clientY,
			translateX,
			translateY
		};
		lastPanSample = { x: event.clientX, y: event.clientY, time: event.timeStamp };
		panVelocity = { x: 0, y: 0 };
	}

	function updatePointerWorld(event: PointerEvent): void {
		const rect = viewportEl?.getBoundingClientRect();
		if (!rect) return;
		pointerWorld = {
			x: (event.clientX - rect.left - translateX) / scale,
			y: (event.clientY - rect.top - translateY) / scale
		};
	}

	function onPointerMove(event: PointerEvent): void {
		updatePointerWorld(event);
		if (dragPointerId === event.pointerId && draggedRootId !== null) {
			const offset = {
				x: clampLineageCoordinate(dragStart.offsetX + (event.clientX - dragStart.x) / scale),
				y: clampLineageCoordinate(dragStart.offsetY + (event.clientY - dragStart.y) / scale)
			};
			dragMoved ||= Math.hypot(event.clientX - dragStart.x, event.clientY - dragStart.y) > 3;
			treeOffsets = { ...treeOffsets, [draggedRootId]: offset };
			return;
		}
		if (pointers.has(event.pointerId)) {
			pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
		}
		if (pinch && pointers.size >= 2 && viewportEl) {
			const [first, second] = [...pointers.values()];
			const distance = Math.hypot(second.x - first.x, second.y - first.y);
			const rect = viewportEl.getBoundingClientRect();
			const midpointX = (first.x + second.x) / 2 - rect.left;
			const midpointY = (first.y + second.y) / 2 - rect.top;
			const nextScale = clampScale(scale * (distance / Math.max(1, pinch.distance)));
			translateX = clampLineageCoordinate(midpointX - pinch.worldX * nextScale);
			translateY = clampLineageCoordinate(midpointY - pinch.worldY * nextScale);
			scale = nextScale;
			pinch = { distance, worldX: pinch.worldX, worldY: pinch.worldY };
			return;
		}
		if (panPointerId !== event.pointerId) return;
		translateX = clampLineageCoordinate(panStart.translateX + event.clientX - panStart.x);
		translateY = clampLineageCoordinate(panStart.translateY + event.clientY - panStart.y);
		if (lastPanSample) {
			const elapsed = event.timeStamp - lastPanSample.time;
			if (elapsed > 0) {
				panVelocity = {
					x: (event.clientX - lastPanSample.x) / elapsed,
					y: (event.clientY - lastPanSample.y) / elapsed
				};
			}
		}
		lastPanSample = { x: event.clientX, y: event.clientY, time: event.timeStamp };
	}

	function onPointerEnd(event: PointerEvent): void {
		if (dragPointerId === event.pointerId && draggedRootId !== null) {
			const rootId = draggedRootId;
			dragPointerId = null;
			draggedRootId = null;
			if (dragMoved) {
				suppressTreeClick = rootId;
				setTimeout(() => {
					if (suppressTreeClick === rootId) suppressTreeClick = null;
				});
				saveLineageTreeOffsets(treeOffsets);
			}
			dragMoved = false;
			return;
		}
		pointers.delete(event.pointerId);
		if (viewportEl?.hasPointerCapture(event.pointerId))
			viewportEl.releasePointerCapture(event.pointerId);
		if (pointers.size < 2) pinch = null;
		if (panPointerId !== event.pointerId) return;
		panPointerId = null;
		lastPanSample = null;
		startInertia();
	}

	function startTreeDrag(event: PointerEvent, rootId: string): void {
		if (event.button !== 0) return;
		event.stopPropagation();
		stopInertia();
		const offset = treeOffsets[rootId] ?? { x: 0, y: 0 };
		dragPointerId = event.pointerId;
		draggedRootId = rootId;
		dragMoved = false;
		dragStart = {
			x: event.clientX,
			y: event.clientY,
			offsetX: offset.x,
			offsetY: offset.y
		};
		(event.currentTarget as HTMLButtonElement).setPointerCapture(event.pointerId);
	}

	function moveTreeFromKeyboard(event: KeyboardEvent, rootId: string): void {
		const directions: Record<string, { x: number; y: number }> = {
			ArrowLeft: { x: -1, y: 0 },
			ArrowRight: { x: 1, y: 0 },
			ArrowUp: { x: 0, y: -1 },
			ArrowDown: { x: 0, y: 1 }
		};
		const direction = directions[event.key];
		if (!direction) return;
		event.preventDefault();
		event.stopPropagation();
		const current = treeOffsets[rootId] ?? { x: 0, y: 0 };
		const step = event.shiftKey ? PAN_STEP : TREE_KEYBOARD_STEP;
		treeOffsets = {
			...treeOffsets,
			[rootId]: {
				x: clampLineageCoordinate(current.x + direction.x * step),
				y: clampLineageCoordinate(current.y + direction.y * step)
			}
		};
		saveLineageTreeOffsets(treeOffsets);
	}

	function resetTreePosition(rootId: string): void {
		const { [rootId]: removed, ...remaining } = treeOffsets;
		void removed;
		treeOffsets = remaining;
		saveLineageTreeOffsets(treeOffsets);
	}

	function resetAllTreePositions(): void {
		treeOffsets = {};
		saveLineageTreeOffsets(treeOffsets);
	}

	function recenterNewest(animate = true): { rootId: string; x: number; y: number } | null {
		const newest = [...persistedRoots].sort(
			(left, right) =>
				right.created_at.localeCompare(left.created_at) || left.id.localeCompare(right.id)
		)[0];
		if (!newest) return null;
		const packed = packedTrees.find((tree) => tree.rootId === newest.id);
		const rootNode = packed?.layout.nodes.find((node) => node.id === packed.layout.rootId);
		if (!packed || !rootNode) return null;
		stopInertia();
		if (animate && !reducedMotion) {
			recentering = true;
			if (recenterTimer) clearTimeout(recenterTimer);
			recenterTimer = setTimeout(() => (recentering = false), 260);
		}
		const x = packed.x + rootNode.x;
		const y = packed.y + rootNode.y;
		translateX = clampLineageCoordinate(viewportWidth / 2 - x * scale);
		translateY = clampLineageCoordinate(viewportHeight / 2 - y * scale);
		return { rootId: newest.id, x, y };
	}

	function restoredViewportIsUsable(): boolean {
		if (
			restoredViewport === null ||
			!Number.isFinite(restoredViewport.translateX) ||
			!Number.isFinite(restoredViewport.translateY) ||
			!Number.isFinite(restoredViewport.scale) ||
			restoredViewport.scale < MIN_SCALE ||
			restoredViewport.scale > MAX_SCALE
		) {
			return false;
		}
		if (
			restoredViewport.rootId !== null &&
			!packedTrees.some((tree) => tree.rootId === restoredViewport.rootId)
		) {
			return false;
		}
		const nearbyPadding = (2 * Math.max(viewportWidth, viewportHeight)) / restoredViewport.scale;
		const restoredRect = viewportWorldRect(
			viewportWidth,
			viewportHeight,
			translateX,
			translateY,
			scale,
			nearbyPadding
		);
		return packedTrees.some((tree) =>
			rectsIntersect(restoredRect, {
				left: tree.x,
				top: tree.y,
				right: tree.x + tree.layout.width,
				bottom: tree.y + tree.layout.height
			})
		);
	}

	function initializeViewport(): void {
		if (viewportReady) return;
		// The roots can arrive before the ResizeObserver has reported a size.
		// Centring against 0x0 puts the newest tree in the corner and nothing
		// revisits it, so wait: the observer calls back here once it has one.
		if (viewportWidth === 0 || viewportHeight === 0) return;
		if (
			restoredViewport?.rootId &&
			!persistedRoots.some((root) => root.id === restoredViewport.rootId) &&
			rootsHaveMore &&
			anchorSearchPages < MAX_ANCHOR_SEARCH_PAGES
		) {
			anchorSearchPages += 1;
			void loadRoots();
			return;
		}
		if (
			restoredViewport?.rootId &&
			restoredViewport.anchorX !== null &&
			restoredViewport.anchorY !== null
		) {
			const currentAnchor = rootWorldPosition(restoredViewport.rootId);
			if (currentAnchor) {
				const rebased = rebaseLineageViewport(
					{ translateX, translateY, scale },
					{ x: restoredViewport.anchorX, y: restoredViewport.anchorY },
					currentAnchor
				);
				translateX = rebased.translateX;
				translateY = rebased.translateY;
			}
		}
		if (!restoredViewportIsUsable()) {
			scale = 1;
			initialViewportAnchor = recenterNewest(false);
		}
		viewportReady = true;
	}

	function onKeyDown(event: KeyboardEvent): void {
		if ((event.target as Element).closest('.lineage-tile.is-root')) return;
		if (event.key === 'ArrowLeft') {
			translateX = clampLineageCoordinate(translateX + PAN_STEP);
		} else if (event.key === 'ArrowRight') {
			translateX = clampLineageCoordinate(translateX - PAN_STEP);
		} else if (event.key === 'ArrowUp') {
			translateY = clampLineageCoordinate(translateY + PAN_STEP);
		} else if (event.key === 'ArrowDown') {
			translateY = clampLineageCoordinate(translateY - PAN_STEP);
		} else if (event.key === '+' || event.key === '=') {
			zoomAt(scale * 1.2, viewportWidth / 2, viewportHeight / 2);
		} else if (event.key === '-' || event.key === '_') {
			zoomAt(scale / 1.2, viewportWidth / 2, viewportHeight / 2);
		} else if (event.key === 'Home') recenterNewest();
		else return;
		event.preventDefault();
	}

	function actionLabel(action: LineageEntry['action']): string {
		return t(`app.lineage.${action}`);
	}

	function modelLabel(data: CanvasNodeData): string {
		const id = data.generation?.model_id ?? data.entry.model_id;
		return (
			studio.models.find((model) => model.id === id)?.name ?? id ?? actionLabel(data.entry.action)
		);
	}

	function promptLabel(data: CanvasNodeData): string {
		return data.generation?.params.prompt?.trim() || actionLabel(data.entry.action);
	}

	function timeLabel(createdAt: string): string {
		return new Intl.DateTimeFormat(getLocale(), {
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		}).format(new Date(createdAt));
	}

	function paramValue(value: unknown): string {
		if (typeof value === 'string') return value;
		if (value === null || typeof value !== 'object') return String(value);
		return JSON.stringify(value);
	}

	async function selectNode(data: CanvasNodeData, origin: HTMLButtonElement): Promise<void> {
		selectionOrigin = origin;
		studio.lineageSelectedAssetId = data.entry.asset_id;
		if (data.generation && generationById(data.generation.id) === undefined) {
			studio.selectedExtra = data.generation;
		}
		if (data.entry.job_id !== null) {
			void selectGeneration(data.entry.job_id);
		} else {
			studio.selectedId = null;
		}
		await tick();
		inspectorEl?.focus();
	}

	function onTileClick(
		event: MouseEvent & { currentTarget: HTMLButtonElement },
		data: CanvasNodeData,
		rootId: string,
		isRoot: boolean
	): void {
		if (isRoot && suppressTreeClick === rootId) {
			suppressTreeClick = null;
			return;
		}
		void selectNode(data, event.currentTarget);
	}

	function closeInspector(): void {
		if (studio.lineageSelectedAssetId === null) return;
		studio.lineageSelectedAssetId = null;
		studio.selectedId = null;
		const target = selectionOrigin?.isConnected ? selectionOrigin : viewportEl;
		selectionOrigin = null;
		void tick().then(() => target?.focus());
	}

	function onWindowKeyDown(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || studio.lineageSelectedAssetId === null) return;
		event.preventDefault();
		closeInspector();
	}

	function preferredModelId(models: typeof studio.models, selectedModelId: string | null): string {
		return (
			models.find((model) => model.id === selectedModelId)?.id ??
			(models.find((model) => model.default) ?? models[0])?.id ??
			''
		);
	}

	function rootWorldPosition(rootId: string): { x: number; y: number } | null {
		const tree = packedTrees.find((item) => item.rootId === rootId);
		const rootNode = tree?.layout.nodes.find((node) => node.id === tree.layout.rootId);
		return tree && rootNode ? { x: tree.x + rootNode.x, y: tree.y + rootNode.y } : null;
	}

	function lineageViewport() {
		if (packedTrees.length === 0) {
			return { translateX, translateY, scale, rootId: null, anchorX: null, anchorY: null };
		}
		const centerX = (viewportWidth / 2 - translateX) / scale;
		const centerY = (viewportHeight / 2 - translateY) / scale;
		let nearest: { id: string; x: number; y: number; distance: number } | null = null;
		for (const tree of packedTrees) {
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
			translateX,
			translateY,
			scale,
			rootId: nearest?.id ?? null,
			anchorX: nearest?.x ?? null,
			anchorY: nearest?.y ?? null
		};
	}

	function saveViewport(): void {
		saveLineageViewport(lineageViewport());
	}

	function openFromSelection(mode: 'generate' | 'image_to_image' | 'upscale'): void {
		if (selectedData === null) return;
		const modelId = selectedGeneration?.model_id ?? selectedData.entry.model_id;
		const params = { ...(selectedGeneration?.params ?? {}) };
		delete params.seed;
		if (mode === 'generate') {
			if (!canGenerateFromPrompt) return;
			studio.modelId = preferredModelId(textToImageModels, modelId);
			studio.generationPrefill = {
				mode,
				sourceAssetId: null,
				prompt: selectedPrompt,
				modelId: studio.modelId,
				params: {}
			};
		} else if (mode === 'image_to_image') {
			if (!canEditSelected || selectedAsset === null) return;
			studio.imageToImageModelId = preferredModelId(imageToImageModels, modelId);
			studio.generationPrefill = {
				mode,
				sourceAssetId: selectedAsset.id,
				prompt: selectedPrompt,
				modelId: studio.imageToImageModelId,
				params
			};
		} else {
			if (!canUpscaleSelected || selectedAsset === null) return;
			studio.upscaleModelId =
				upscaleModels.find((model) => model.id === modelId)?.id ??
				defaultUpscaleModelId(studio.models);
			studio.generationPrefill = {
				mode,
				sourceAssetId: selectedAsset.id,
				prompt: selectedPrompt,
				modelId: studio.upscaleModelId,
				params
			};
		}
		studio.prompt = selectedPrompt;
		saveViewport();
		openService(mode);
	}

	function imageUrl(data: CanvasNodeData, band: typeof lod): string | null {
		if (data.entry.missing || failedImageIds.has(data.entry.asset_id)) return null;
		if (band === 'cards') {
			return data.generation?.assets[0]?.url ?? data.entry.thumbnail_url;
		}
		return data.entry.thumbnail_url;
	}

	function replaceGeneration(assetId: string, generation: Generation): void {
		roots = roots.map((root) => (root.id === generation.id ? generation : root));
		let changed = false;
		const nextCache = new Map(treeCache);
		for (const [rootId, cached] of nextCache) {
			if (!cached.layout?.nodes.some((node) => node.id === assetId)) continue;
			const nodes = cached.layout.nodes.map((node) =>
				node.id === assetId
					? {
							...node,
							data: {
								...node.data,
								entry: {
									...node.data.entry,
									thumbnail_url: generation.assets[0]?.thumbnail_url ?? null,
									missing: generation.assets.length === 0
								},
								generation
							}
						}
					: node
			);
			nextCache.set(rootId, { ...cached, layout: { ...cached.layout, nodes } });
			changed = true;
		}
		if (changed) treeCache = nextCache;
	}

	async function refreshImage(data: CanvasNodeData): Promise<void> {
		const assetId = data.entry.asset_id;
		if (data.entry.job_id === null || refreshedImageIds.has(assetId)) {
			failedImageIds = new Set([...failedImageIds, assetId]);
			return;
		}
		refreshedImageIds.add(assetId);
		refreshingImageIds = new Set([...refreshingImageIds, assetId]);
		try {
			const generation = await fetchCanvasJson<Generation>(
				`/api/v1/generations/${data.entry.job_id}`,
				'generation refresh failed'
			);
			failedImageIds = new Set([...failedImageIds].filter((id) => id !== assetId));
			replaceGeneration(assetId, generation);
		} catch (error) {
			if (error instanceof DOMException && error.name === 'AbortError') return;
			failedImageIds = new Set([...failedImageIds, assetId]);
		} finally {
			refreshingImageIds = new Set([...refreshingImageIds].filter((id) => id !== assetId));
		}
	}

	function proximityScale(node: VisibleNode): number {
		if (!pointerWorld || focusedNodeId === node.node.id) return 1;
		const distance = Math.hypot(node.x - pointerWorld.x, node.y - pointerWorld.y);
		if (distance >= HOVER_RADIUS) return 1;
		const pull = (1 - distance / HOVER_RADIUS) ** 2;
		return 1 + pull * HOVER_PULL;
	}

	function canvasInteractions(node: HTMLDivElement) {
		node.addEventListener('wheel', onWheel, { passive: false });
		node.addEventListener('keydown', onKeyDown);
		node.addEventListener('pointerdown', onPointerDown);
		node.addEventListener('pointermove', onPointerMove);
		node.addEventListener('pointerup', onPointerEnd);
		node.addEventListener('pointercancel', onPointerEnd);
		node.addEventListener('lostpointercapture', onPointerEnd);
		window.addEventListener('pointerup', onPointerEnd);
		window.addEventListener('pointercancel', onPointerEnd);
		const clearPointer = () => (pointerWorld = null);
		node.addEventListener('pointerleave', clearPointer);
		return () => {
			node.removeEventListener('wheel', onWheel);
			node.removeEventListener('keydown', onKeyDown);
			node.removeEventListener('pointerdown', onPointerDown);
			node.removeEventListener('pointermove', onPointerMove);
			node.removeEventListener('pointerup', onPointerEnd);
			node.removeEventListener('pointercancel', onPointerEnd);
			node.removeEventListener('lostpointercapture', onPointerEnd);
			window.removeEventListener('pointerup', onPointerEnd);
			window.removeEventListener('pointercancel', onPointerEnd);
			node.removeEventListener('pointerleave', clearPointer);
		};
	}

	onMount(() => {
		reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		const resize = new ResizeObserver(([entry]) => {
			viewportWidth = entry.contentRect.width;
			viewportHeight = entry.contentRect.height;
			if (!viewportReady && rootsInitialized && viewportWidth > 0 && viewportHeight > 0) {
				initializeViewport();
			}
		});
		if (viewportEl) resize.observe(viewportEl);
		void loadRoots();
		return () => resize.disconnect();
	});

	onDestroy(() => {
		if (viewportReady) saveViewport();
		canvasActive = false;
		for (const controller of requestControllers) controller.abort();
		requestControllers.clear();
		stopInertia();
		if (initializeFrame) cancelAnimationFrame(initializeFrame);
		if (recenterTimer) clearTimeout(recenterTimer);
		if (viewportSaveTimer) clearTimeout(viewportSaveTimer);
	});
</script>

<svelte:window onkeydown={onWindowKeyDown} />

<div
	class="lineage-section relative grid h-full min-h-0 grid-cols-[minmax(0,1fr)_auto] grid-rows-[auto_minmax(0,1fr)] gap-3"
>
	<header class="col-span-2 shrink-0">
		<h1 class="text-xl font-semibold">{t('app.images.title')}</h1>
		<p class="text-muted-foreground mt-1 text-sm">{t('app.images.sub')}</p>
	</header>
	<!-- A focusable canvas region owns the documented pan and zoom keyboard controls. -->
	<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
	<div
		bind:this={viewportEl}
		{@attach canvasInteractions}
		class="lineage-viewport border-border bg-card/20 relative col-start-1 row-start-2 min-h-0 min-w-0 overflow-hidden rounded-lg border"
		class:is-panning={panPointerId !== null || pinch !== null || dragPointerId !== null}
		role="application"
		aria-label={t('app.images.canvas')}
		tabindex="0"
	>
		<div class="absolute end-3 top-3 z-30 flex gap-1">
			<Button
				variant={starredOnly ? 'default' : 'secondary'}
				size="sm"
				aria-pressed={starredOnly}
				onclick={() => setStarredOnly(!starredOnly)}
			>
				<StarIcon class={starredOnly ? 'fill-current' : ''} />
				{t('app.images.starred_only')}
			</Button>
			<Button
				variant="secondary"
				size="icon-sm"
				disabled={!hasTreeOffsets}
				title={t('app.images.reset_all_positions')}
				aria-label={t('app.images.reset_all_positions')}
				onclick={resetAllTreePositions}
			>
				<RotateCcwIcon />
			</Button>
			<Button
				variant="secondary"
				size="icon-sm"
				title={t('app.images.zoom_out')}
				aria-label={t('app.images.zoom_out')}
				onclick={() => zoomAt(scale / 1.2, viewportWidth / 2, viewportHeight / 2)}
			>
				<MinusIcon />
			</Button>
			<Button
				variant="secondary"
				size="icon-sm"
				title={t('app.images.zoom_in')}
				aria-label={t('app.images.zoom_in')}
				onclick={() => zoomAt(scale * 1.2, viewportWidth / 2, viewportHeight / 2)}
			>
				<PlusIcon />
			</Button>
			<Button
				variant="secondary"
				size="icon-sm"
				title={t('app.images.recenter')}
				aria-label={t('app.images.recenter')}
				onclick={() => recenterNewest()}
			>
				<LocateFixedIcon />
			</Button>
		</div>

		{#if rootsLoading && roots.length === 0}
			<div class="text-muted-foreground absolute inset-0 grid place-items-center text-sm">
				<span class="flex items-center gap-2">
					<LoaderCircleIcon class="size-4 animate-spin motion-reduce:animate-none" />
					{t('app.images.loading')}
				</span>
			</div>
		{:else if rootsFailed}
			<div class="absolute inset-0 z-20 grid place-items-center text-sm">
				<div class="flex flex-col items-center gap-3">
					<span class="text-muted-foreground">{t('app.images.load_failed')}</span>
					<Button variant="secondary" size="sm" onclick={() => void loadRoots()}>
						{t('app.images.retry')}
					</Button>
				</div>
			</div>
		{:else if persistedRoots.length === 0}
			<div class="text-muted-foreground absolute inset-0 grid place-items-center text-sm">
				{starredOnly ? t('app.images.no_starred_roots') : t('app.gen.result_hint')}
			</div>
		{/if}

		<div
			class={`lineage-world lod-${lod}`}
			class:recentering
			style={`transform: translate3d(${translateX}px, ${translateY}px, 0) scale(${scale})`}
		>
			<svg class="lineage-edges" aria-hidden="true">
				<defs>
					<marker
						id="lineage-arrow"
						viewBox="0 0 8 8"
						refX="7"
						refY="4"
						markerWidth="8"
						markerHeight="8"
						markerUnits="userSpaceOnUse"
						orient="auto"
					>
						<path d="M 0 0 L 8 4 L 0 8 z" />
					</marker>
				</defs>
				{#each packedTrees as tree (tree.rootId)}
					{#each tree.layout.edges as edge (edge.id)}
						{@const edgeLeft = tree.x + Math.min(edge.source.x, edge.target.x)}
						{@const edgeTop = tree.y + Math.min(edge.source.y, edge.target.y)}
						{@const edgeRight = tree.x + Math.max(edge.source.x, edge.target.x)}
						{@const edgeBottom = tree.y + Math.max(edge.source.y, edge.target.y)}
						{#if rectsIntersect( worldRect, { left: edgeLeft, top: edgeTop, right: edgeRight, bottom: edgeBottom } )}
							<g
								class:is-active={selectedPathEdgeIds.has(edge.id)}
								class:is-dimmed={shouldDimLineageEdge(
									selectedNode !== null,
									selectedPathEdgeIds.has(edge.id)
								)}
							>
								<path
									class="lineage-edge"
									d={lineageEdgePath(edge.source, edge.target, tree.x, tree.y)}
									marker-end="url(#lineage-arrow)"
								/>
								<text
									x={tree.x + (edge.source.x + edge.target.x) / 2}
									y={tree.y + (edge.source.y + edge.target.y) / 2 - 7}
								>
									{actionLabel(edge.target.data.entry.action)}
								</text>
							</g>
						{/if}
					{/each}
				{/each}
			</svg>

			{#each packedTrees as tree (tree.rootId)}
				<span class="cluster-time" style={`transform: translate(${tree.x}px, ${tree.y - 24}px)`}>
					{timeLabel(tree.createdAt)}
				</span>
			{/each}

			{#each visibleNodes as item (`${item.rootId}:${item.node.id}`)}
				{@const data = item.node.data}
				{@const shownImage = imageUrl(data, lod)}
				{@const starred = data.entry.job_id !== null && isStarred(data.entry.job_id)}
				<div
					class="tile-shell"
					class:is-new={newNodeIds.has(item.node.id)}
					style={`transform: translate3d(${item.x - LINEAGE_TILE_WIDTH / 2}px, ${item.y - LINEAGE_TILE_HEIGHT / 2}px, 0)`}
				>
					<button
						type="button"
						class="lineage-tile"
						class:is-root={item.isRoot}
						class:is-dragging={draggedRootId === item.rootId}
						class:is-selected={studio.lineageSelectedAssetId === data.entry.asset_id}
						class:is-missing={data.entry.missing || shownImage === null}
						style={`--tile-pull: ${proximityScale(item)}`}
						aria-label={`${actionLabel(data.entry.action)}: ${promptLabel(data)}${starred ? `. ${t('app.images.starred')}` : ''}${item.isRoot ? `. ${t('app.images.drag_tree')}` : ''}${item.treeStatus === 'loading' ? `. ${t('app.images.tree_loading')}` : ''}`}
						title={promptLabel(data)}
						onfocus={() => (focusedNodeId = item.node.id)}
						onblur={() => (focusedNodeId = null)}
						onpointerdown={(event) => {
							if (item.isRoot) startTreeDrag(event, item.rootId);
						}}
						onkeydown={(event) => {
							if (item.isRoot) moveTreeFromKeyboard(event, item.rootId);
						}}
						onclick={(event) => onTileClick(event, data, item.rootId, item.isRoot)}
					>
						{#if item.isRoot}
							<span
								class="root-affordance"
								title={item.treeStatus === 'loading'
									? t('app.images.tree_loading')
									: t('app.images.drag_tree')}
								aria-hidden="true"
							>
								{#if item.treeStatus === 'loading'}
									<LoaderCircleIcon class="animate-spin motion-reduce:animate-none" />
								{:else}
									<MoveIcon />
								{/if}
							</span>
						{/if}
						<span class="micro-content">
							{#if shownImage !== null}
								{#key shownImage}
									<img
										src={lod === 'constellation' ? shownImage : undefined}
										alt=""
										draggable="false"
										onerror={() => void refreshImage(data)}
									/>
								{/key}
							{:else}
								<ImageOffIcon />
							{/if}
						</span>
						<span class="tree-content">
							{#if starred}
								<span class="node-star" aria-hidden="true"><StarIcon /></span>
							{/if}
							{#if shownImage !== null}
								{#key shownImage}
									<img
										src={lod === 'trees' ? shownImage : undefined}
										alt=""
										draggable="false"
										onerror={() => void refreshImage(data)}
									/>
								{/key}
							{:else}
								<ImageOffIcon />
							{/if}
						</span>
						<span class="card-content">
							{#if starred}
								<span class="node-star" aria-hidden="true"><StarIcon /></span>
							{/if}
							<span class="card-image">
								{#if shownImage !== null}
									{#key shownImage}
										<img
											src={lod === 'cards' ? shownImage : undefined}
											alt=""
											draggable="false"
											onerror={() => void refreshImage(data)}
										/>
									{/key}
								{:else if refreshingImageIds.has(data.entry.asset_id)}
									<LoaderCircleIcon class="animate-spin motion-reduce:animate-none" />
								{:else}
									<ImageOffIcon />
								{/if}
							</span>
							<span class="card-copy">
								<strong>{modelLabel(data)}</strong>
								<span>{promptLabel(data)}</span>
								<time datetime={data.entry.created_at}>{timeLabel(data.entry.created_at)}</time>
							</span>
						</span>
					</button>
					{#if item.isRoot && treeOffsets[item.rootId] !== undefined}
						<button
							type="button"
							class="reset-tree-position"
							title={t('app.images.reset_tree_position')}
							aria-label={t('app.images.reset_tree_position')}
							onpointerdown={(event) => event.stopPropagation()}
							onclick={(event) => {
								event.stopPropagation();
								resetTreePosition(item.rootId);
							}}
						>
							<RotateCcwIcon />
						</button>
					{/if}
					{#if item.isRoot && item.remainingCountLowerBound > 0}
						<span class="truncated-count">
							{t('app.images.truncated_more').replace(
								'{count}',
								String(item.remainingCountLowerBound)
							)}
						</span>
					{/if}
				</div>
			{/each}
		</div>
	</div>

	{#if selectedData !== null}
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<aside
			bind:this={inspectorEl}
			class="selection-inspector border-border bg-card absolute inset-y-0 end-0 z-40 row-start-2 flex w-[min(22rem,calc(100%-1rem))] flex-col overflow-y-auto rounded-lg border p-4 shadow-xl md:relative md:inset-auto md:z-auto md:col-start-2 md:w-80 md:shadow-none xl:w-96"
			aria-labelledby="selection-inspector-title"
			tabindex="-1"
		>
			<div class="mb-4 flex items-start justify-between gap-3">
				<div>
					<h2 id="selection-inspector-title" class="font-semibold">
						{t('app.images.inspector_title')}
					</h2>
					<p class="text-muted-foreground mt-1 text-xs">
						{actionLabel(selectedData.entry.action)}
					</p>
				</div>
				<Button
					variant="ghost"
					size="icon-sm"
					title={t('app.images.close_inspector')}
					aria-label={t('app.images.close_inspector')}
					onclick={closeInspector}
				>
					<XIcon />
				</Button>
			</div>

			{#if selectedImageUrl !== null}
				<a
					href={selectedImageUrl}
					target="_blank"
					rel="noopener"
					class="bg-muted mb-4 block aspect-square shrink-0 overflow-hidden rounded-lg"
					title={t('app.gen.open_full')}
				>
					<img
						src={selectedImageUrl}
						alt={selectedPrompt || t('app.gen.result')}
						class="h-full w-full object-contain"
					/>
				</a>
			{:else}
				<div
					class="border-border text-muted-foreground mb-4 grid aspect-square shrink-0 place-items-center rounded-lg border border-dashed"
				>
					<span class="flex flex-col items-center gap-2 text-sm">
						<ImageOffIcon class="size-8" />
						{t('app.lineage.missing')}
					</span>
				</div>
			{/if}

			<div class="border-border mb-4 grid gap-2 border-b pb-4">
				<Button
					variant="outline"
					class="justify-start"
					disabled={selectedGeneration === null}
					onclick={() => void toggleSelectedStar()}
				>
					<StarIcon class={selectedIsStarred ? 'fill-current' : ''} />
					{selectedIsStarred ? t('app.gen.unstar') : t('app.gen.star')}
				</Button>
				<Button
					href={selectedAsset?.download_url}
					variant="outline"
					class="justify-start"
					disabled={!selectedHasBytes}
				>
					<DownloadIcon />
					{t('app.gen.download')}
				</Button>
				<Button
					variant="outline"
					class="justify-start"
					disabled={!canUpscaleSelected}
					onclick={() => openFromSelection('upscale')}
				>
					<ScanLineIcon />
					{t('app.images.action_upscale')}
				</Button>
				<Button
					variant="outline"
					class="justify-start"
					disabled={!canEditSelected}
					onclick={() => openFromSelection('image_to_image')}
				>
					<PencilIcon />
					{t('app.images.action_edit')}
				</Button>
				<Button
					variant="outline"
					class="justify-start"
					disabled={!canGenerateFromPrompt}
					onclick={() => openFromSelection('generate')}
				>
					<WandSparklesIcon />
					{t('app.images.action_generate_prompt')}
				</Button>
			</div>

			<dl class="grid gap-3 text-sm">
				<div>
					<dt class="text-muted-foreground text-xs">{t('app.gen.prompt')}</dt>
					<dd class="mt-1 whitespace-pre-wrap">{selectedPrompt || t('app.images.no_prompt')}</dd>
				</div>
				<div class="grid grid-cols-2 gap-3">
					<div>
						<dt class="text-muted-foreground text-xs">{t('app.gen.model')}</dt>
						<dd class="mt-1">{modelLabel(selectedData)}</dd>
					</div>
					<div>
						<dt class="text-muted-foreground text-xs">{t('app.images.action')}</dt>
						<dd class="mt-1">{actionLabel(selectedData.entry.action)}</dd>
					</div>
				</div>
				<div>
					<dt class="text-muted-foreground text-xs">{t('app.images.created')}</dt>
					<dd class="mt-1">
						<time datetime={selectedData.entry.created_at}>
							{timeLabel(selectedData.entry.created_at)}
						</time>
					</dd>
				</div>
				{#if selectedParams.length > 0}
					<div>
						<dt class="text-muted-foreground text-xs">{t('app.images.parameters')}</dt>
						<dd class="mt-2 grid grid-cols-2 gap-x-4 gap-y-2">
							{#each selectedParams as [key, value] (key)}
								<span class="text-muted-foreground">{key}</span>
								<span class="min-w-0 break-words text-end font-mono text-xs">
									{paramValue(value)}
								</span>
							{/each}
						</dd>
					</div>
				{/if}
			</dl>
		</aside>
	{/if}
</div>

<style>
	.lineage-viewport {
		touch-action: none;
		cursor: grab;
		background-image:
			linear-gradient(
				to right,
				color-mix(in oklch, var(--border) 55%, transparent) 1px,
				transparent 1px
			),
			linear-gradient(
				to bottom,
				color-mix(in oklch, var(--border) 55%, transparent) 1px,
				transparent 1px
			);
		background-size: 32px 32px;
	}

	.lineage-viewport.is-panning {
		cursor: grabbing;
	}

	.lineage-viewport:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.lineage-world {
		position: absolute;
		inset: 0 auto auto 0;
		width: 0;
		height: 0;
		transform-origin: 0 0;
		will-change: transform;
	}

	.lineage-world.recentering {
		transition: transform 240ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	.lineage-edges {
		position: absolute;
		inset: 0;
		width: 1px;
		height: 1px;
		overflow: visible;
		pointer-events: none;
	}

	.lineage-edge {
		fill: none;
		stroke: color-mix(in oklch, var(--muted-foreground) 55%, var(--border));
		stroke-width: 2;
		vector-effect: non-scaling-stroke;
	}

	#lineage-arrow path {
		fill: context-stroke;
	}

	.lineage-edges g {
		opacity: 0.8;
		transition: opacity 160ms ease;
	}

	.lineage-edges g.is-active {
		opacity: 1;
	}

	.lineage-edges g.is-active .lineage-edge {
		stroke: var(--primary);
		stroke-width: 3;
	}

	.lineage-edges g.is-dimmed {
		opacity: 0.16;
	}

	.lineage-edges text {
		fill: var(--muted-foreground);
		font-size: 11px;
		text-anchor: middle;
		paint-order: stroke;
		stroke: var(--background);
		stroke-width: 5px;
		stroke-linejoin: round;
	}

	.lod-constellation .lineage-edges text {
		display: none;
	}

	.cluster-time {
		position: absolute;
		width: max-content;
		color: var(--muted-foreground);
		font-size: 11px;
		line-height: 1;
		pointer-events: none;
		display: none;
	}

	.lod-constellation .cluster-time {
		display: block;
	}

	.tile-shell {
		--visible-tile-half-width: 108px;
		--visible-tile-half-height: 88px;
		position: absolute;
		width: 216px;
		height: 176px;
		display: grid;
		place-items: center;
		pointer-events: none;
	}

	.lineage-tile {
		--tile-pull: 1;
		pointer-events: auto;
		position: relative;
		display: block;
		padding: 0;
		color: var(--foreground);
		background: var(--card);
		border: 1px solid var(--border);
		transform: scale(var(--tile-pull));
		transition: transform 120ms cubic-bezier(0.16, 1, 0.3, 1);
		transform-origin: center;
	}

	.lineage-tile:active {
		transform: scale(calc(var(--tile-pull) * 0.98));
	}

	.lineage-tile.is-root {
		cursor: move;
	}

	.lineage-tile.is-root.is-dragging {
		cursor: grabbing;
	}

	.root-affordance {
		position: absolute;
		top: 4px;
		right: 4px;
		z-index: 1;
		display: grid;
		width: 20px;
		height: 20px;
		place-items: center;
		border-radius: 4px;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--card) 82%, transparent);
		pointer-events: none;
	}

	.root-affordance :global(svg) {
		width: 12px;
		height: 12px;
	}

	.reset-tree-position {
		position: absolute;
		top: calc(50% - var(--visible-tile-half-height) - 10px);
		left: calc(50% + var(--visible-tile-half-width) - 10px);
		z-index: 2;
		display: grid;
		width: 24px;
		height: 24px;
		padding: 0;
		place-items: center;
		border: 1px solid var(--border);
		border-radius: 999px;
		color: var(--foreground);
		background: var(--card);
		box-shadow: 0 2px 8px color-mix(in oklch, var(--foreground) 14%, transparent);
		pointer-events: auto;
	}

	.reset-tree-position:hover {
		background: var(--accent);
	}

	.reset-tree-position:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.reset-tree-position :global(svg) {
		width: 13px;
		height: 13px;
	}

	.truncated-count {
		position: absolute;
		top: calc(50% + var(--visible-tile-half-height) + 6px);
		left: 50%;
		width: max-content;
		max-width: 200px;
		transform: translateX(-50%);
		color: var(--muted-foreground);
		font-size: 11px;
		line-height: 1.2;
		text-align: center;
		pointer-events: none;
	}

	.lineage-tile:focus-visible {
		outline: 3px solid var(--ring);
		outline-offset: 3px;
		transform: scale(1);
	}

	.lineage-tile.is-selected {
		border-color: var(--primary);
	}

	.selection-inspector:focus {
		outline: none;
	}

	.micro-content,
	.tree-content,
	.card-content {
		display: none;
	}

	.lod-constellation .lineage-tile {
		width: 36px;
		height: 36px;
	}

	.lod-constellation .tile-shell {
		--visible-tile-half-width: 18px;
		--visible-tile-half-height: 18px;
	}

	.lod-constellation .micro-content {
		display: grid;
		width: 100%;
		height: 100%;
		place-items: center;
	}

	.micro-content img,
	.tree-content img,
	.card-image img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.micro-content :global(svg) {
		width: 14px;
		height: 14px;
		color: var(--muted-foreground);
	}

	.lod-trees .lineage-tile {
		width: 104px;
		height: 104px;
	}

	.lod-trees .tile-shell {
		--visible-tile-half-width: 52px;
		--visible-tile-half-height: 52px;
	}

	.lod-trees .tree-content {
		display: grid;
		width: 100%;
		height: 100%;
		place-items: center;
	}

	.tree-content :global(svg) {
		width: 24px;
		height: 24px;
		color: var(--muted-foreground);
	}

	.node-star {
		position: absolute;
		top: 5px;
		left: 5px;
		z-index: 2;
		display: grid;
		width: 22px;
		height: 22px;
		place-items: center;
		border-radius: 999px;
		color: var(--primary-foreground);
		background: var(--primary);
		box-shadow: 0 1px 4px color-mix(in oklch, var(--foreground) 20%, transparent);
		pointer-events: none;
	}

	.node-star :global(svg) {
		width: 12px;
		height: 12px;
		fill: currentColor;
		color: inherit;
	}

	.lod-cards .lineage-tile {
		width: 216px;
		height: 176px;
		text-align: left;
	}

	.lod-cards .card-content {
		display: grid;
		width: 100%;
		height: 100%;
		grid-template-columns: 88px minmax(0, 1fr);
	}

	.card-image {
		display: grid;
		min-width: 0;
		place-items: center;
		background: var(--muted);
		overflow: hidden;
	}

	.card-image :global(svg) {
		width: 24px;
		height: 24px;
		color: var(--muted-foreground);
	}

	.card-copy {
		display: flex;
		min-width: 0;
		flex-direction: column;
		gap: 8px;
		padding: 12px;
	}

	.card-copy strong,
	.card-copy span,
	.card-copy time {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.card-copy strong {
		font-size: 12px;
		white-space: nowrap;
	}

	.card-copy span {
		display: -webkit-box;
		font-size: 12px;
		line-height: 1.35;
		color: var(--muted-foreground);
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 4;
		line-clamp: 4;
	}

	.card-copy time {
		margin-top: auto;
		font-size: 10px;
		color: var(--muted-foreground);
		white-space: nowrap;
	}

	.tile-shell.is-new .lineage-tile {
		animation: tile-arrive 220ms cubic-bezier(0.16, 1, 0.3, 1) both;
	}

	@keyframes tile-arrive {
		from {
			opacity: 0;
			transform: translateX(-18px) scale(0.92);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.lineage-world.recentering,
		.lineage-tile,
		.lineage-edges g {
			transition: none;
		}

		.tile-shell.is-new .lineage-tile {
			animation: none;
		}
	}
</style>
