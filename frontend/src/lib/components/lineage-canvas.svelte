<script module lang="ts">
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
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import RotateCcwIcon from '@lucide/svelte/icons/rotate-ccw';
	import ScanLineIcon from '@lucide/svelte/icons/scan-line';
	import SearchIcon from '@lucide/svelte/icons/search';
	import StarIcon from '@lucide/svelte/icons/star';
	import WandSparklesIcon from '@lucide/svelte/icons/wand-sparkles';
	import XIcon from '@lucide/svelte/icons/x';
	import GitForkIcon from '@lucide/svelte/icons/git-fork';
	import LayoutGridIcon from '@lucide/svelte/icons/layout-grid';
	import { useStudioHeader } from '$lib/studio-chrome.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import { toggleStarWithUndo } from '$lib/studio-toasts';
	import { revealOnLoad } from '$lib/image-reveal';
	import { MediaQuery } from 'svelte/reactivity';
	import { beforeNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { closeOnHistoryStep, forgetOverlay, popOverlay, pushOverlay } from '$lib/overlay-history';
	import { attachSwipeDismiss } from '$lib/swipe-dismiss';
	import { getLocale, t } from '$lib/i18n.svelte';
	import { formatMs } from '$lib/benchmark';
	import ImageFilterControls from '$lib/components/image-filter-controls.svelte';
	import {
		defaultImageFilters,
		generationMatchesImageFilters,
		imageFiltersActive,
		imageFiltersEqual,
		withImageFilters,
		type ImageFilters,
		type ImageModelOption
	} from '$lib/image-filters';
	import {
		clampLineageCoordinate,
		buildLineageRover,
		decideInitialLineageViewportFollow,
		decideViewportScheduleAfterRootPage,
		lineageFocusActiveKind,
		lineageMountedIds,
		lineageRoverEntryId,
		lineageTileTabIndex,
		shouldRestoreLineageTileFocus,
		nextLineageRoverId,
		rebaseLineageViewport,
		retainedLineageTreeOffsets,
		settleLineageRootStarReconciliation,
		shouldPanToFocusedNode,
		shouldSpendAnchorSearchPage,
		translationToCenterNode,
		type InitialLineageViewportAnchor,
		type LineageRootStarReconciliation
	} from '$lib/lineage-canvas-state';
	import {
		LineageForest,
		type LineageNodeData as CanvasNodeData,
		type LineageTree as CachedTree
	} from '$lib/lineage-forest';
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
	import { account } from '$lib/account.svelte';
	import { studioCache } from '$lib/swr-cache';
	import {
		GRID_RESUME_LIMIT,
		imagesGridSnapshotKey,
		makeGridVisitSnapshot,
		mayPrependLiveGridRows,
		restoredGridAnchor,
		rowsForSnapshot,
		selectionInGridWindow,
		snapshotMatchesCriteria,
		type GridResumeAnchor,
		type GridVisitSnapshot
	} from '$lib/images-grid-resume';
	import { dragScroll } from '$lib/drag-scroll';
	import { sectionNeeds } from '$lib/account-display';
	import { collapsePromptDiff, paramDeltas, promptWordDiff } from '$lib/prompt-diff';
	import {
		lineageSearchMatchPositions,
		lineageSearchMatchRootIds,
		lineageSearchTone,
		nextLineageMatchIndex
	} from '$lib/lineage-search';
	import {
		defaultUpscaleModelId,
		filterImageToImageModels,
		filterTextToImageModels,
		filterUpscaleModels,
		generationById,
		getImagesPreferences,
		isStarred,
		openService,
		saveLineageTreeOffsets,
		saveLineageViewport,
		saveImagesPreferences,
		selectGeneration,
		viewOwnerEpoch,
		studio,
		toggleStarred,
		type Generation,
		type GenerationSubtree,
		type LineageEntry
	} from '$lib/studio.svelte';

	const ROOT_LIMIT = 50;
	const restoredImagesPreferences = getImagesPreferences();
	const componentOwnerEpoch = viewOwnerEpoch();
	const gridSnapshotKey = imagesGridSnapshotKey(componentOwnerEpoch);
	function copyImageFilters(filters: ImageFilters): ImageFilters {
		return {
			models: [...filters.models],
			sizes: [...filters.sizes],
			range: filters.range,
			from: filters.from,
			through: filters.through
		};
	}
	// A saved viewport names the root it was anchored to, and that root may sit
	// several pages back. Hunting it costs one round trip per page with nothing
	// drawn yet, and it never resolves at all when the anchor has since expired,
	// so give up after this many and open on the newest instead.
	const MAX_ANCHOR_SEARCH_PAGES = 4;
	const MAX_MOUNTED_TILES = 600;
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
	class InvalidGridResume extends Error {}
	const restoredViewport = studio.lineageViewport;

	let sectionEl = $state<HTMLDivElement | null>(null);
	let imagesHeaderEl = $state<HTMLDivElement | null>(null);
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
	let rootsFailedAppend = $state(false);
	let rootsHaveMore = $state(false);
	let rootsCommittedFilterModeEpoch = $state<number | null>(null);
	// Shared with the sidebar's Starred entry, which opens this view filtered.
	let starredOnly = $state(restoredImagesPreferences.starredOnly);
	let imageFilters = $state<ImageFilters>(copyImageFilters(restoredImagesPreferences.filters));
	let persistedImageFilters = copyImageFilters(restoredImagesPreferences.filters);
	let imageFilterReferenceDate = $state(new Date());
	let imageModelOptions = $state<ImageModelOption[]>([]);
	let imageModelsLoading = $state(true);
	let imageModelsFailed = $state(false);
	let gridServerRows = $state<Generation[]>([]);
	let gridServerRowsQueryKey = $state<string | null>(null);
	let gridLiveRows = $state<Generation[]>([]);
	let gridLoading = $state(false);
	let gridFailed = $state(false);
	let gridHasMore = $state(false);
	let gridServerCursor = $state<string | null>(null);
	let gridFailedAppend = $state(false);
	let gridScrollerEl = $state<HTMLDivElement | null>(null);
	let gridScrollAnchor: { id: string; top: number } | null = null;
	let gridScrollIntentEpoch = 0;
	let gridRequestEpoch = 0;
	let gridRequestController: AbortController | null = null;
	let searchDraft = $state(restoredImagesPreferences.search);
	let searchQuery = $state(restoredImagesPreferences.search);
	let searchMatchIds = $state<Set<string> | null>(null);
	let searchMatchIndex = $state(-1);
	let searchRequestEpoch = 0;
	let searchCriteriaKey = '';
	let searchDebounce: ReturnType<typeof setTimeout> | undefined;
	let rootsFilterModeEpoch = 0;
	let rootsRequestEpoch = 0;
	let rootsRequestController: AbortController | null = null;
	let rootStarReconciliation: LineageRootStarReconciliation = { pending: 0, dirty: false };
	let recenterAfterFilter = false;
	let anchorSearchPages = 0;
	let initializeFrame = 0;
	let treeCache = $state(new Map<string, CachedTree>());
	let newNodeIds = $state(new Set<string>());
	let failedImageIds = $state(new Set<string>());
	let refreshingImageIds = $state(new Set<string>());
	let refreshedImageIds = new Set<string>();
	let pointerWorld = $state<{ x: number; y: number } | null>(null);
	let focusedNodeId = $state<string | null>(null);
	let expandedEdgeIds = $state(new Set<string>());
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
	const forest = new LineageForest({
		fetchSubtree: fetchLineageSubtree,
		initialHistory: studio.history,
		history: () => studio.history,
		onTreesChanged: (trees) => {
			if (viewOwnerEpoch() === componentOwnerEpoch) treeCache = new Map(trees);
		},
		onRootLoaded: (root) => {
			if (viewOwnerEpoch() !== componentOwnerEpoch) return;
			roots = roots.map((item) => (item.id === root.id ? { ...root } : item));
		},
		onArrived: markArrived
	});
	treeCache = new Map(forest.trees);

	const persistedRoots = $derived(
		roots.filter((root) => root.assets.length > 0 || root.expired === true)
	);
	const completedHistorySignature = $derived(
		studio.history
			.filter((generation) => generation.assets.length > 0)
			.map((generation) => generation.id)
			.sort()
			.join('\0')
	);
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
	const searchMatchRootIds = $derived(lineageSearchMatchRootIds(packedTrees, searchMatchIds));
	const hasTreeOffsets = $derived(Object.keys(treeOffsets).length > 0);
	const worldNodes = $derived.by(() => {
		const shown: VisibleNode[] = [];
		for (const packed of packedTrees) {
			for (const node of packed.layout.nodes) {
				shown.push({
					rootId: packed.rootId,
					x: packed.x + node.x,
					y: packed.y + node.y,
					isRoot: node.id === packed.layout.rootId,
					treeStatus: treeCache.get(packed.rootId)?.status ?? null,
					remainingCountLowerBound: treeCache.get(packed.rootId)?.remainingCountLowerBound ?? 0,
					node
				});
			}
		}
		return shown;
	});
	const visibleNodes = $derived.by(() => {
		const mounted = lineageMountedIds(
			worldNodes.map((item) => ({
				id: item.node.id,
				inView: rectsIntersect(worldRect, {
					left: item.x - LINEAGE_TILE_WIDTH / 2,
					top: item.y - LINEAGE_TILE_HEIGHT / 2,
					right: item.x + LINEAGE_TILE_WIDTH / 2,
					bottom: item.y + LINEAGE_TILE_HEIGHT / 2
				})
			})),
			focusedNodeId,
			MAX_MOUNTED_TILES
		);
		return worldNodes.filter((item) => mounted.has(item.node.id));
	});
	const lineageRover = $derived(
		buildLineageRover(
			packedTrees.map((tree) => ({
				rootId: tree.layout.rootId,
				createdAt: tree.createdAt,
				nodes: tree.layout.nodes.map((node) => ({
					id: node.id,
					createdAt: node.data.entry.created_at
				})),
				edges: tree.layout.edges.map((edge) => ({
					parentId: edge.source.id,
					childId: edge.target.id
				}))
			}))
		)
	);
	const forestBottom = $derived(
		Math.max(0, ...packedTrees.map((packed) => packed.y + packed.layout.height))
	);
	const forestRight = $derived(
		Math.max(0, ...packedTrees.map((packed) => packed.x + packed.layout.width))
	);
	// Grid lays the loaded history out as a plain grid; Lineage is the canvas
	// of trees. Both read data the studio already loads.
	type ImagesMode = 'grid' | 'lineage';
	let imagesMode = $state<ImagesMode>(restoredImagesPreferences.mode);
	type GridFilter = 'all' | 'generate' | 'image_to_image' | 'upscale';
	let gridFilter = $state<GridFilter>(restoredImagesPreferences.action);
	let gridSelectedId = $state<string | null>(null);
	let requestedGridSelectionId = restoredImagesPreferences.gridSelectionId;
	let pendingGridResumeAnchor: GridResumeAnchor | null = restoredImagesPreferences.gridAnchor;
	let gridWindowBeforeCursor: string | null = pendingGridResumeAnchor?.beforeCursor ?? null;
	let gridResumeFallbackUsed = false;
	let gridMiddleWindow = $state(false);
	let gridSnapshotSeededCriteria = '';
	let pendingLineageSelection = restoredImagesPreferences.lineageSelection;
	let gridAnchorSaveTimer: ReturnType<typeof setTimeout> | undefined;
	let gridAnchorSaveTimerScroller: HTMLElement | null = null;
	let gridScrollElement: HTMLElement | null = null;
	let gridLastObservedScrollTop: number | null = null;
	let gridNavigationAnchor: {
		ownerEpoch: number;
		criteriaKey: string;
		rows: Generation[];
		anchor: GridResumeAnchor;
	} | null = null;

	function persistImagesPreferences(): void {
		if (viewOwnerEpoch() !== componentOwnerEpoch) return;
		saveImagesPreferences({
			...getImagesPreferences(),
			mode: imagesMode,
			filters: persistedImageFilters,
			action: gridFilter,
			search: searchQuery,
			starredOnly,
			gridAnchor: pendingGridResumeAnchor,
			gridSelectionId: gridSelectedId ?? requestedGridSelectionId,
			lineageSelection: pendingLineageSelection
		});
	}

	function saveViewportForOwner(viewport: ReturnType<typeof lineageViewport>): void {
		if (viewOwnerEpoch() === componentOwnerEpoch) saveLineageViewport(viewport);
	}

	function saveTreeOffsetsForOwner(offsets: Record<string, { x: number; y: number }>): void {
		if (viewOwnerEpoch() === componentOwnerEpoch) saveLineageTreeOffsets(offsets);
	}

	function capturePersistedGridAnchor(): GridResumeAnchor | null {
		const scroller = gridScrollerEl;
		if (!scroller || gridServerRowsQueryKey !== gridCriteriaKey) return null;
		const scrollerTop = scroller.getBoundingClientRect().top;
		for (const tile of scroller.querySelectorAll<HTMLButtonElement>('.grid-tile')) {
			const rect = tile.getBoundingClientRect();
			const id = tile.dataset.generationId;
			if (
				!id ||
				rect.bottom <= scrollerTop ||
				rect.top >= scroller.getBoundingClientRect().bottom
			) {
				continue;
			}
			const rowIndex = gridServerRows.findIndex((row) => row.id === id);
			if (rowIndex === -1) continue;
			return {
				id,
				beforeCursor: gridServerRows[rowIndex - 1]?.id ?? gridWindowBeforeCursor,
				offset: rect.top - scrollerTop
			};
		}
		return null;
	}

	function saveGridAnchorSoon(): void {
		const scroller = gridScrollerEl;
		if (!scroller) return;
		if (gridScrollElement === scroller) gridLastObservedScrollTop = scroller.scrollTop;
		if (gridAnchorSaveTimer !== undefined) clearTimeout(gridAnchorSaveTimer);
		gridAnchorSaveTimerScroller = scroller;
		const timer = setTimeout(() => {
			if (gridAnchorSaveTimer === timer) {
				gridAnchorSaveTimer = undefined;
				gridAnchorSaveTimerScroller = null;
			}
			if (
				viewOwnerEpoch() !== componentOwnerEpoch ||
				imagesMode !== 'grid' ||
				!scroller.isConnected ||
				gridScrollerEl !== scroller
			)
				return;
			const anchor = capturePersistedGridAnchor();
			if (anchor === null) return;
			pendingGridResumeAnchor = anchor;
			persistImagesPreferences();
		}, 250);
		gridAnchorSaveTimer = timer;
	}

	function hasGridAnchorCandidate(
		candidate: typeof gridNavigationAnchor
	): candidate is NonNullable<typeof gridNavigationAnchor> {
		return (
			candidate !== null &&
			viewOwnerEpoch() === componentOwnerEpoch &&
			candidate.ownerEpoch === componentOwnerEpoch &&
			(imageFilterKey === null || candidate.criteriaKey === gridCriteriaKey) &&
			candidate.criteriaKey === gridServerRowsQueryKey &&
			candidate.rows === gridServerRows
		);
	}

	function captureGridAnchorBeforeExit(): GridResumeAnchor | null {
		const scroller = gridScrollerEl;
		if (
			viewOwnerEpoch() !== componentOwnerEpoch ||
			!scroller?.isConnected ||
			gridScrollElement !== scroller
		)
			return null;
		const scrollChanged =
			gridLastObservedScrollTop !== null && scroller.scrollTop !== gridLastObservedScrollTop;
		const timerPendingForScroller =
			gridAnchorSaveTimer !== undefined && gridAnchorSaveTimerScroller === scroller;
		return timerPendingForScroller || scrollChanged ? capturePersistedGridAnchor() : null;
	}

	function setImagesMode(mode: ImagesMode): void {
		if (imagesMode === mode) return;
		if (imagesMode === 'grid' && mode === 'lineage') {
			const anchor = captureGridAnchorBeforeExit();
			gridNavigationAnchor =
				anchor !== null && gridServerRowsQueryKey !== null
					? {
							ownerEpoch: componentOwnerEpoch,
							criteriaKey: gridServerRowsQueryKey,
							rows: gridServerRows,
							anchor
						}
					: null;
		} else if (mode === 'grid') {
			if (hasGridAnchorCandidate(gridNavigationAnchor))
				pendingGridResumeAnchor = gridNavigationAnchor.anchor;
			gridNavigationAnchor = null;
		}
		// The inspector belongs to the mode it opened in: close it, and its
		// history entry, rather than leave a selection to reopen unmarked later.
		const outgoing = imagesMode === 'grid' ? gridSelectedId : studio.lineageSelectedAssetId;
		if (outgoing !== null) {
			stopLeaving();
			void popOverlay('inspector');
			selectionOrigin = null;
			if (imagesMode === 'grid') {
				gridSelectedId = null;
			} else {
				studio.lineageSelectedAssetId = null;
				studio.selectedId = null;
			}
		}
		if (imagesMode === 'lineage') pendingLineageSelection = null;
		gridSelectedId = null;
		requestedGridSelectionId = null;
		imagesMode = mode;
		gridScrollAnchor = null;
		persistImagesPreferences();
		if (mode === 'lineage') reloadRoots();
		else {
			cancelRootsRequest();
			recenterAfterFilter = false;
		}
	}

	beforeNavigate(() => {
		if (viewOwnerEpoch() !== componentOwnerEpoch) return;
		const candidate = gridNavigationAnchor;
		const anchor =
			imagesMode === 'grid'
				? captureGridAnchorBeforeExit()
				: hasGridAnchorCandidate(candidate)
					? candidate.anchor
					: null;
		if (anchor === null) return;
		pendingGridResumeAnchor = anchor;
		persistImagesPreferences();
	});

	function generationAction(generation: Generation): LineageEntry['action'] {
		if (generation.source_asset_id === null) return 'generate';
		return studio.models
			.find((model) => model.id === generation.model_id)
			?.capabilities.includes('upscale')
			? 'upscale'
			: 'image_to_image';
	}

	function generationMatchesGridAction(generation: Generation): boolean {
		return generationMatchesGridActionFor(generation, gridFilter) === true;
	}

	function generationMatchesGridKnownImageFilters(
		generation: Generation,
		filters: ImageFilters = imageFilters,
		referenceDate: Date = imageFilterReferenceDate
	): boolean {
		if (withImageFilters(new URLSearchParams(), filters, referenceDate) === null) return true;
		return generationMatchesImageFilters(generation, { ...filters, sizes: [] }, referenceDate);
	}

	function generationMatchesRetainedGridFilters(
		generation: Generation,
		filters: ImageFilters,
		filter: GridFilter,
		starred: boolean
	): boolean {
		return (
			generationMatchesGridKnownImageFilters(generation, filters) &&
			generationMatchesGridActionFor(generation, filter) !== false &&
			(!starred || generation.starred_at !== null || isStarred(generation.id))
		);
	}

	function attachGridDragScroll(scroller: HTMLElement): () => void {
		gridCriteriaKey;
		imagesMode;
		gridScrollElement = scroller;
		gridLastObservedScrollTop = scroller.scrollTop;
		const stopDragScroll = dragScroll(scroller, () => {
			gridScrollAnchor = null;
		});
		const onPointerDown = (event: PointerEvent) => {
			if (event.pointerType !== 'touch') noteGridScrollIntent();
		};
		const onTouchStart = () => noteGridScrollIntent();
		scroller.addEventListener('pointerdown', onPointerDown, { passive: true });
		scroller.addEventListener('touchstart', onTouchStart, { passive: true });
		scroller.addEventListener('wheel', noteGridScrollIntent, { passive: true });
		scroller.addEventListener('keydown', onGridKeydown);
		return () => {
			stopDragScroll();
			scroller.removeEventListener('pointerdown', onPointerDown);
			scroller.removeEventListener('touchstart', onTouchStart);
			scroller.removeEventListener('wheel', noteGridScrollIntent);
			scroller.removeEventListener('keydown', onGridKeydown);
			if (gridScrollElement === scroller) {
				gridScrollElement = null;
				gridLastObservedScrollTop = null;
			}
		};
	}

	function generationMatchesGridActionFor(
		generation: Generation,
		filter: GridFilter
	): boolean | null {
		if (filter === 'all') return true;
		if (filter === 'generate') return generation.source_asset_id === null;
		if (generation.source_asset_id === null) return false;
		const model = studio.models.find((item) => item.id === generation.model_id);
		if (model === undefined) return null;
		return (model.capabilities.includes('upscale') ? 'upscale' : 'image_to_image') === filter;
	}

	function captureGridScrollAnchor(
		filters: ImageFilters,
		filter: GridFilter = gridFilter,
		starred: boolean = starredOnly
	): { id: string; top: number } | null {
		if (!gridScrollerEl) return null;
		const scroller = gridScrollerEl.getBoundingClientRect();
		let fallback: { id: string; top: number } | null = null;
		for (const tile of gridScrollerEl.querySelectorAll<HTMLButtonElement>('.grid-tile')) {
			const rect = tile.getBoundingClientRect();
			if (rect.bottom <= scroller.top || rect.top >= scroller.bottom) continue;
			const id = tile.dataset.generationId;
			const generation = gridItems.find((item) => item.id === id);
			if (
				id &&
				generation &&
				generationMatchesRetainedGridFilters(generation, filters, filter, starred)
			) {
				const anchor = { id, top: rect.top };
				fallback ??= anchor;
				if (
					filters.sizes.length === 0 ||
					generationMatchesImageFilters(generation, filters, imageFilterReferenceDate)
				)
					return anchor;
			}
		}
		return fallback;
	}

	function restorePersistedGridAnchor(anchor: GridResumeAnchor | null, intentEpoch: number): void {
		if (
			!anchor ||
			!canvasActive ||
			viewOwnerEpoch() !== componentOwnerEpoch ||
			intentEpoch !== gridScrollIntentEpoch ||
			!gridScrollerEl
		)
			return;
		restoreGridScrollAnchor({
			id: anchor.id,
			top: gridScrollerEl.getBoundingClientRect().top + anchor.offset
		});
	}

	function noteGridScrollIntent(): void {
		gridScrollIntentEpoch += 1;
		gridScrollAnchor = null;
	}

	function onGridKeydown(event: KeyboardEvent): void {
		if (['ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', 'Home', 'End', ' '].includes(event.key))
			noteGridScrollIntent();
	}

	function restoreGridScrollAnchor(anchor: { id: string; top: number } | null): void {
		if (!anchor || !gridScrollerEl) return;
		const tile = gridScrollerEl.querySelector<HTMLButtonElement>(
			`[data-generation-id="${CSS.escape(anchor.id)}"]`
		);
		if (!tile) return;
		gridScrollerEl.scrollTop += tile.getBoundingClientRect().top - anchor.top;
	}

	function gridCriteriaFor(filterKey: string | null): string {
		return JSON.stringify({
			filters: filterKey,
			query: searchQuery,
			starred: starredOnly,
			action: gridFilter
		});
	}

	const imageFilterKey = $derived(
		withImageFilters(new URLSearchParams(), imageFilters, imageFilterReferenceDate)?.toString() ??
			null
	);
	const gridCriteriaKey = $derived(gridCriteriaFor(imageFilterKey));
	const gridServerPageLoaded = $derived(gridServerRowsQueryKey !== null);

	function seedGridSnapshot(criteriaKey: string): void {
		if (gridSnapshotSeededCriteria === criteriaKey) return;
		gridSnapshotSeededCriteria = criteriaKey;
		const snapshot = studioCache.read<GridVisitSnapshot>(gridSnapshotKey);
		if (!snapshotMatchesCriteria(snapshot, criteriaKey)) return;
		gridServerRows = rowsForSnapshot(snapshot, criteriaKey);
		gridServerRowsQueryKey = criteriaKey;
		gridWindowBeforeCursor = snapshot.beforeCursor;
		gridServerCursor = snapshot.cursor;
		gridHasMore = snapshot.hasMore;
		gridMiddleWindow = snapshot.window === 'cursor';
		if (pendingGridResumeAnchor === null) {
			gridSelectedId = selectionInGridWindow(gridServerRows, requestedGridSelectionId);
		}
		const anchor = restoredGridAnchor(gridServerRows, pendingGridResumeAnchor);
		if (anchor) {
			gridScrollAnchor = {
				id: anchor.id,
				top: (gridScrollerEl?.getBoundingClientRect().top ?? 0) + anchor.offset
			};
		}
		const intentEpoch = gridScrollIntentEpoch;
		void tick().then(() => restorePersistedGridAnchor(anchor, intentEpoch));
	}

	const gridItems = $derived.by(() => {
		const serverIds = new Set(gridServerRows.map((generation) => generation.id));
		const liveById = new Map(gridLiveRows.map((generation) => [generation.id, generation]));
		const orderTime = (generation: Generation) => {
			const timestamp = Date.parse(
				starredOnly ? (generation.starred_at ?? '') : generation.created_at
			);
			return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY;
		};
		const compareServerOrder = (left: Generation, right: Generation) =>
			orderTime(right) - orderTime(left) || right.id.localeCompare(left.id);
		const firstServerRow = gridServerRows[0];
		const newLiveRows = gridLiveRows
			.filter(() => mayPrependLiveGridRows(gridMiddleWindow ? 'cursor' : 'newest'))
			.filter((generation) => !serverIds.has(generation.id) && generation.assets.length > 0)
			.filter((generation) =>
				firstServerRow ? compareServerOrder(generation, firstServerRow) < 0 : gridServerPageLoaded
			)
			.sort(compareServerOrder);
		const seen = new Set<string>();
		return [
			...newLiveRows,
			...gridServerRows.map((generation) => liveById.get(generation.id) ?? generation)
		]
			.filter((generation) => {
				if (generation.assets.length === 0 || seen.has(generation.id)) return false;
				seen.add(generation.id);
				return true;
			})
			.filter((generation) => {
				if (imageFilterKey === null) return true;
				const isCurrentServerResult =
					serverIds.has(generation.id) && gridServerRowsQueryKey === gridCriteriaKey;
				if (isCurrentServerResult) return true;
				const isLiveOnly = !serverIds.has(generation.id);
				return isLiveOnly
					? generation.state === 'succeeded' &&
							generationMatchesImageFilters(generation, imageFilters, imageFilterReferenceDate) &&
							generationMatchesGridActionFor(generation, gridFilter) === true &&
							(!starredOnly || generation.starred_at !== null || isStarred(generation.id))
					: generationMatchesRetainedGridFilters(generation, imageFilters, gridFilter, starredOnly);
			});
	});

	// Lineage ignores the action filter, so there it always shows everything.
	const allFilterOn = $derived(
		!starredOnly &&
			(imagesMode === 'lineage' || gridFilter === 'all') &&
			!imageFiltersActive(imageFilters)
	);

	const gridSelectedData = $derived.by<CanvasNodeData | null>(() => {
		if (gridSelectedId === null) return null;
		const generation = gridItems.find((row) => row.id === gridSelectedId);
		if (!generation || generation.assets.length === 0) return null;
		const asset = generation.assets[0];
		return {
			output_asset_ids: generation.assets.map((item) => item.id),
			entry: {
				job_id: generation.id,
				asset_id: asset.id,
				action: generationAction(generation),
				model_id: generation.model_id,
				created_at: generation.created_at,
				state: generation.state,
				thumbnail_url: asset.thumbnail_url,
				missing: generation.expired === true
			},
			generation
		};
	});

	async function loadGridPage(
		append: boolean,
		resumeAnchor: GridResumeAnchor | null = null,
		cursorResume = resumeAnchor !== null
	): Promise<void> {
		if (imagesMode !== 'grid' || gridLoading) return;
		const pageLimit = resumeAnchor ? GRID_RESUME_LIMIT : 50;
		const query = withImageFilters(
			new URLSearchParams({ limit: String(pageLimit) }),
			imageFilters,
			imageFilterReferenceDate
		);
		if (query === null) {
			return;
		}
		const requestKey = gridCriteriaKey;
		const resumingWarmWindow =
			resumeAnchor !== null && gridServerRowsQueryKey === requestKey && gridServerRows.length > 0;
		if (searchQuery !== '') query.append('q', searchQuery);
		if (starredOnly) query.set('starred', 'true');
		if (gridFilter !== 'all') query.set('action', gridFilter);
		const cursor = append
			? gridServerCursor
			: cursorResume
				? (resumeAnchor?.beforeCursor ?? null)
				: null;
		if (cursor) query.set('cursor', cursor);
		query.append('state', 'succeeded');
		const epoch = gridRequestEpoch;
		const scrollIntentEpoch = gridScrollIntentEpoch;
		const warmResumeWasSuperseded = () =>
			resumingWarmWindow &&
			(!canvasActive ||
				epoch !== gridRequestEpoch ||
				viewOwnerEpoch() !== componentOwnerEpoch ||
				scrollIntentEpoch !== gridScrollIntentEpoch);
		gridRequestController?.abort();
		const controller = new AbortController();
		gridRequestController = controller;
		requestControllers.add(controller);
		gridLoading = true;
		gridFailed = false;
		gridFailedAppend = false;
		let invalidResume = false;
		let pageCursorResume = cursorResume;
		let restoredAnchor: GridResumeAnchor | null = null;
		let restoredSelectionId: string | null = null;
		try {
			const response = await fetch(`/api/v1/generations?${query}`, { signal: controller.signal });
			if (warmResumeWasSuperseded()) return;
			if (!response.ok) {
				if (resumeAnchor && response.status === 404) invalidResume = true;
				else throw new Error('grid request failed');
			}
			if (invalidResume) throw new InvalidGridResume();
			let page = (await response.json()) as Generation[];
			if (warmResumeWasSuperseded()) return;
			if (!canvasActive || epoch !== gridRequestEpoch || viewOwnerEpoch() !== componentOwnerEpoch)
				return;
			restoredAnchor = resumeAnchor ? restoredGridAnchor(page, resumeAnchor) : null;
			if (
				resumingWarmWindow &&
				!pageCursorResume &&
				resumeAnchor?.beforeCursor &&
				restoredAnchor === null
			) {
				const cursorQuery = new URLSearchParams(query);
				cursorQuery.set('cursor', resumeAnchor.beforeCursor);
				const cursorResponse = await fetch(`/api/v1/generations?${cursorQuery}`, {
					signal: controller.signal
				});
				if (warmResumeWasSuperseded()) return;
				if (!cursorResponse.ok) {
					if (cursorResponse.status === 404) invalidResume = true;
					else throw new Error('grid cursor recovery failed');
				}
				if (invalidResume) throw new InvalidGridResume();
				page = (await cursorResponse.json()) as Generation[];
				if (warmResumeWasSuperseded()) return;
				pageCursorResume = true;
				restoredAnchor = restoredGridAnchor(page, resumeAnchor);
			}
			if (pageCursorResume && resumeAnchor && restoredAnchor === null) {
				invalidResume = true;
				throw new InvalidGridResume();
			}
			const activeSelectionId = gridSelectedId;
			gridServerRows = append ? [...gridServerRows, ...page] : page;
			if (!append) gridServerRowsQueryKey = requestKey;
			if (!append) gridServerCursor = page.at(-1)?.id ?? null;
			else if (page.length > 0) gridServerCursor = page.at(-1)?.id ?? gridServerCursor;
			gridHasMore = page.length === pageLimit;
			if (resumeAnchor) {
				if (scrollIntentEpoch === gridScrollIntentEpoch)
					pendingGridResumeAnchor = restoredAnchor ? resumeAnchor : null;
				gridWindowBeforeCursor = pageCursorResume ? resumeAnchor.beforeCursor : null;
				gridMiddleWindow = pageCursorResume && resumeAnchor.beforeCursor !== null;
			} else if (!append) {
				gridWindowBeforeCursor = null;
				gridMiddleWindow = false;
			}
			if (!append) {
				if (
					activeSelectionId !== null &&
					gridItems.some((generation) => generation.id === activeSelectionId)
				) {
					requestedGridSelectionId = activeSelectionId;
				} else {
					gridSelectedId = selectionInGridWindow(page, requestedGridSelectionId);
					requestedGridSelectionId = gridSelectedId;
					if (activeSelectionId === null) restoredSelectionId = gridSelectedId;
				}
			}
			if (viewOwnerEpoch() === componentOwnerEpoch && requestKey === gridCriteriaKey) {
				studioCache.write(
					gridSnapshotKey,
					makeGridVisitSnapshot({
						criteriaKey: requestKey,
						rows: gridServerRows,
						beforeCursor: gridWindowBeforeCursor,
						hasMore: gridHasMore,
						window: gridMiddleWindow ? 'cursor' : 'newest'
					})
				);
				persistImagesPreferences();
			}
			if (!append) {
				await tick();
				if (
					epoch === gridRequestEpoch &&
					canvasActive &&
					viewOwnerEpoch() === componentOwnerEpoch
				) {
					if (restoredAnchor) restorePersistedGridAnchor(restoredAnchor, scrollIntentEpoch);
					else if (scrollIntentEpoch === gridScrollIntentEpoch)
						restoreGridScrollAnchor(gridScrollAnchor);
					gridScrollAnchor = null;
					if (restoredSelectionId !== null && gridSelectedId === restoredSelectionId) {
						inspectorOpened();
						if (
							!sectionEl?.contains(document.activeElement) &&
							!imagesHeaderEl?.contains(document.activeElement)
						)
							inspectorEl?.focus({ preventScroll: true });
					}
				}
			}
		} catch (error) {
			if (warmResumeWasSuperseded()) return;
			if (error instanceof InvalidGridResume) {
				invalidResume = true;
			} else {
				if (error instanceof DOMException && error.name === 'AbortError') return;
				if (epoch !== gridRequestEpoch || viewOwnerEpoch() !== componentOwnerEpoch) return;
				if (!append) gridScrollAnchor = null;
				gridFailedAppend = append;
				gridFailed = true;
			}
		} finally {
			requestControllers.delete(controller);
			if (gridRequestController === controller) gridRequestController = null;
			if (epoch === gridRequestEpoch && viewOwnerEpoch() === componentOwnerEpoch)
				gridLoading = false;
		}
		if (
			invalidResume &&
			epoch === gridRequestEpoch &&
			canvasActive &&
			viewOwnerEpoch() === componentOwnerEpoch &&
			!gridResumeFallbackUsed
		) {
			gridResumeFallbackUsed = true;
			pendingGridResumeAnchor = null;
			gridMiddleWindow = false;
			gridSelectedId = null;
			requestedGridSelectionId = null;
			gridScrollAnchor = null;
			studioCache.delete(gridSnapshotKey);
			persistImagesPreferences();
			gridRequestEpoch += 1;
			gridLoading = false;
			gridServerRows = [];
			gridServerRowsQueryKey = null;
			gridWindowBeforeCursor = null;
			gridServerCursor = null;
			gridHasMore = false;
			void loadGridPage(false);
		}
	}

	function reloadGrid(): void {
		gridScrollAnchor ??= captureGridScrollAnchor(imageFilters);
		gridRequestEpoch += 1;
		gridRequestController?.abort();
		gridFailed = false;
		gridLoading = false;
		const resumeAnchor = pendingGridResumeAnchor;
		if (resumeAnchor) gridResumeFallbackUsed = false;
		const warmNewestResume =
			resumeAnchor !== null &&
			gridServerRowsQueryKey === gridCriteriaKey &&
			gridServerRows.length > 0 &&
			!gridMiddleWindow &&
			gridServerRows.slice(0, GRID_RESUME_LIMIT).some((row) => row.id === resumeAnchor.id);
		void loadGridPage(false, resumeAnchor, !warmNewestResume);
	}

	function loadNewestGridPage(): void {
		if (!gridMiddleWindow || gridLoading) return;
		pendingGridResumeAnchor = null;
		gridScrollAnchor = null;
		gridSelectedId = null;
		requestedGridSelectionId = null;
		persistImagesPreferences();
		gridRequestEpoch += 1;
		gridRequestController?.abort();
		gridFailed = false;
		gridLoading = false;
		void loadGridPage(false);
	}

	function setImageFilters(next: ImageFilters): void {
		if (imageFiltersEqual(imageFilters, next)) return;
		const referenceDate = new Date();
		const nextFilter = withImageFilters(new URLSearchParams(), next, referenceDate);
		const canApply = nextFilter !== null;
		if (canApply) {
			if (gridNavigationAnchor?.criteriaKey !== gridCriteriaFor(nextFilter.toString()))
				gridNavigationAnchor = null;
			gridScrollAnchor = captureGridScrollAnchor(next);
			pendingGridResumeAnchor = null;
			gridMiddleWindow = false;
			persistedImageFilters = copyImageFilters(next);
			persistImagesPreferences();
		}
		imageFilters = next;
		imageFilterReferenceDate = referenceDate;
		const anchor = gridScrollAnchor;
		const scrollIntentEpoch = gridScrollIntentEpoch;
		if (canApply)
			void tick().then(() => {
				if (
					canvasActive &&
					viewOwnerEpoch() === componentOwnerEpoch &&
					scrollIntentEpoch === gridScrollIntentEpoch
				)
					restoreGridScrollAnchor(anchor);
			});
		rootsFilterModeEpoch += 1;
		if (imagesMode === 'lineage' && canApply) {
			reloadRoots();
		} else if (imagesMode === 'lineage') {
			cancelRootsRequest();
			recenterAfterFilter = false;
		}
	}

	$effect(() => {
		gridCriteriaKey;
		imagesMode;
		if (imagesMode === 'grid' && imageFilterKey !== null) {
			untrack(() => {
				seedGridSnapshot(gridCriteriaKey);
				reloadGrid();
			});
		} else {
			gridRequestEpoch += 1;
			gridRequestController?.abort();
			gridLoading = false;
		}
	});

	$effect(() => {
		const saved = pendingLineageSelection;
		if (!saved || imagesMode !== 'lineage' || viewOwnerEpoch() !== componentOwnerEpoch) return;
		const root = persistedRoots.find((item) => item.id === saved.rootId);
		if (root) {
			if (studio.lineageSelectedAssetId === saved.assetId) return;
			untrack(() => forest.ensureVisible(root, layoutLineageTree(rootLayoutNode(root))));
			const cached = treeCache.get(root.id);
			if (cached?.status === 'loaded' && cached.layout) {
				const node = cached.layout.nodes.find((item) => item.id === saved.assetId);
				const missingRoot = cached.layout.nodes.find(
					(item) => item.data.entry.job_id === saved.assetId && item.data.entry.missing
				);
				if (node || missingRoot) {
					studio.lineageSelectedAssetId = saved.assetId;
					void tick().then(() => {
						if (
							!canvasActive ||
							viewOwnerEpoch() !== componentOwnerEpoch ||
							studio.lineageSelectedAssetId !== saved.assetId
						)
							return;
						inspectorOpened();
						if (
							!sectionEl?.contains(document.activeElement) &&
							!imagesHeaderEl?.contains(document.activeElement)
						)
							inspectorEl?.focus({ preventScroll: true });
					});
				} else if (!cached.truncated) {
					pendingLineageSelection = null;
					persistImagesPreferences();
				}
				return;
			}
			return;
		}
		if (
			rootsInitialized &&
			!rootsLoading &&
			(!rootsHaveMore || anchorSearchPages >= MAX_ANCHOR_SEARCH_PAGES)
		) {
			pendingLineageSelection = null;
			persistImagesPreferences();
		}
	});

	$effect(() => {
		const history = studio.history;
		const filters = imageFilters;
		const referenceDate = imageFilterReferenceDate;
		const query = searchQuery;
		const serverMatches = searchMatchIds;
		const starred = starredOnly;
		if (imageFilterKey === null) return;
		const matching = history.filter(
			(generation) =>
				generation.assets.length > 0 &&
				(query === '' || serverMatches?.has(generation.id) === true) &&
				(!starred || isStarred(generation.id)) &&
				generationMatchesImageFilters(generation, filters, referenceDate) &&
				generationMatchesGridAction(generation)
		);
		gridLiveRows = matching;
		if (
			gridSelectedId !== null &&
			!matching.some((item) => item.id === gridSelectedId) &&
			!gridServerRows.some((item) => item.id === gridSelectedId && item.assets.length > 0)
		) {
			gridSelectedId = null;
			requestedGridSelectionId = null;
			persistImagesPreferences();
		}
	});

	useStudioHeader(() => ({ actions: imagesHeader }));

	const selectedNode = $derived.by(() => {
		if (studio.lineageSelectedAssetId === null) return null;
		for (const tree of packedTrees) {
			const node = tree.layout.nodes.find((item) => item.id === studio.lineageSelectedAssetId);
			if (node) return node;
			const missingRoot = tree.layout.nodes.find(
				(item) =>
					item.data.entry.job_id === studio.lineageSelectedAssetId && item.data.entry.missing
			);
			if (missingRoot) return missingRoot;
		}
		return null;
	});
	const selectedData = $derived(
		imagesMode === 'grid' ? gridSelectedData : (selectedNode?.data ?? null)
	);
	const selectedGeneration = $derived(
		selectedData?.generation ??
			(selectedData?.entry.job_id ? (generationById(selectedData.entry.job_id) ?? null) : null)
	);
	const selectedIsStarred = $derived(
		selectedGeneration !== null && isStarred(selectedGeneration.id)
	);
	const selectedAsset = $derived(
		selectedGeneration?.assets.find(
			(asset) =>
				asset.id ===
				(imagesMode === 'grid' ? selectedData?.entry.asset_id : studio.lineageSelectedAssetId)
		) ?? null
	);
	const selectedImageUrl = $derived(
		selectedData !== null && !selectedData.entry.missing ? (selectedAsset?.url ?? null) : null
	);
	const selectedThumbnailUrl = $derived(
		selectedImageUrl === null
			? null
			: (selectedAsset?.thumbnail_url ?? selectedData?.entry.thumbnail_url ?? null)
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
	// Every service a node opens creates a job, which a viewer cannot do.
	const mayCreate = $derived(sectionNeeds('generate', account.current?.role ?? null) === null);
	const canGenerateFromPrompt = $derived(
		mayCreate && selectedPrompt !== '' && textToImageModels.length > 0
	);
	const canEditSelected = $derived(mayCreate && selectedHasBytes && imageToImageModels.length > 0);
	const canUpscaleSelected = $derived(mayCreate && selectedHasBytes && upscaleModels.length > 0);
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
		const assetId = asset?.id ?? root.id;
		return {
			id: assetId,
			createdAt: root.created_at,
			data: {
				output_asset_ids: root.assets.map((item) => item.id),
				entry: {
					job_id: root.id,
					asset_id: assetId,
					action: 'generate',
					model_id: root.model_id,
					created_at: root.created_at,
					state: root.state,
					thumbnail_url: asset?.thumbnail_url ?? null,
					missing: root.expired === true || asset === undefined
				},
				generation: root
			},
			children: []
		};
	}

	async function fetchLineageSubtree(
		rootId: string,
		signal: AbortSignal
	): Promise<GenerationSubtree> {
		const response = await fetch(`/api/v1/generations/${rootId}/subtree`, { signal });
		if (!response.ok) throw new Error('root subtree request failed');
		return response.json() as Promise<GenerationSubtree>;
	}

	async function fetchCanvasJson<T>(
		url: string,
		failure: string,
		controller = new AbortController()
	): Promise<T> {
		requestControllers.add(controller);
		try {
			const response = await fetch(url, { signal: controller.signal });
			if (!response.ok) throw new Error(failure);
			const value = (await response.json()) as T;
			if (
				!canvasActive ||
				canvasEpoch !== canvasEpochSequence ||
				viewOwnerEpoch() !== componentOwnerEpoch
			) {
				throw new DOMException('stale canvas request', 'AbortError');
			}
			return value;
		} finally {
			requestControllers.delete(controller);
		}
	}

	async function loadRoots(replace = false): Promise<void> {
		if (
			imagesMode !== 'lineage' ||
			imageFilterKey === null ||
			rootsLoading ||
			(!replace && rootsCommittedFilterModeEpoch !== rootsFilterModeEpoch) ||
			(!replace && !rootsHaveMore && roots.length > 0)
		)
			return;
		const controller = new AbortController();
		rootsRequestController = controller;
		const filterModeEpoch = rootsFilterModeEpoch;
		const requestEpoch = rootsRequestEpoch;
		const filterStarredOnly = starredOnly;
		// Every place below that acts on this request's outcome checks this: the
		// finally block, the scheduling guard, and both frame callbacks. The
		// guard covers the decision, the callback covers the frame delay, and a
		// reload in between would otherwise let a superseded frame mark the
		// viewport ready.
		const stillCurrent = () =>
			canvasActive &&
			canvasEpoch === canvasEpochSequence &&
			viewOwnerEpoch() === componentOwnerEpoch &&
			filterModeEpoch === rootsFilterModeEpoch &&
			requestEpoch === rootsRequestEpoch;
		rootsLoading = true;
		rootsFailed = false;
		rootsFailedAppend = false;
		const cursor = replace ? undefined : roots.at(-1)?.id;
		let loaded = false;
		try {
			const query = new URLSearchParams({ roots_only: 'true', limit: String(ROOT_LIMIT) });
			if (filterStarredOnly) query.set('starred', 'true');
			const filteredQuery = withImageFilters(query, imageFilters, imageFilterReferenceDate);
			if (filteredQuery === null) return;
			if (cursor !== undefined) filteredQuery.set('cursor', cursor);
			filteredQuery.append('state', 'succeeded');
			// Starred mode selects starred roots, then keeps each returned root's
			// complete subtree. Filtering descendants individually would sever the
			// provenance that makes this a forest rather than a list of cards.
			const page = await fetchCanvasJson<Generation[]>(
				`/api/v1/generations?${filteredQuery.toString()}`,
				'history request failed',
				controller
			);
			if (filterModeEpoch !== rootsFilterModeEpoch || requestEpoch !== rootsRequestEpoch) {
				return;
			}
			if (replace) roots = page;
			else {
				const existing = new Set(roots.map((root) => root.id));
				roots = [...roots, ...page.filter((root) => !existing.has(root.id))];
			}
			if (replace) rootsCommittedFilterModeEpoch = filterModeEpoch;
			rootsHaveMore = page.length === ROOT_LIMIT;
			rootsInitialized = true;
			loaded = true;
		} catch (error) {
			if (error instanceof DOMException && error.name === 'AbortError') return;
			if (filterModeEpoch === rootsFilterModeEpoch && requestEpoch === rootsRequestEpoch) {
				rootsFailed = true;
				rootsFailedAppend = !replace && cursor !== undefined;
				if (replace) rootsHaveMore = false;
			}
		} finally {
			if (rootsRequestController === controller) rootsRequestController = null;
			if (stillCurrent()) {
				rootsLoading = false;
			}
		}
		if (stillCurrent()) {
			const schedule = decideViewportScheduleAfterRootPage(
				loaded,
				viewportReady,
				recenterAfterFilter,
				persistedRoots.length > 0
			);
			if (schedule === 'initialize') {
				initializeFrame = requestAnimationFrame(() => {
					if (!stillCurrent()) return;
					initializeViewport();
				});
			} else if (schedule === 'recenter') {
				recenterAfterFilter = false;
				initializeFrame = requestAnimationFrame(() => {
					if (!stillCurrent()) return;
					initialViewportAnchor = recenterNewest(false);
				});
			}
		}
	}

	function cancelRootsRequest(): void {
		rootsRequestEpoch += 1;
		rootsRequestController?.abort();
		rootsRequestController = null;
		rootsLoading = false;
	}

	function reloadRoots(force = false): void {
		if (force) rootsCommittedFilterModeEpoch = null;
		if (imagesMode !== 'lineage' || imageFilterKey === null) {
			cancelRootsRequest();
			recenterAfterFilter = false;
			return;
		}
		if (!force && rootsCommittedFilterModeEpoch === rootsFilterModeEpoch) {
			recenterAfterFilter = false;
			return;
		}
		cancelRootsRequest();
		rootsFailed = false;
		rootsFailedAppend = false;
		recenterAfterFilter = viewportReady;
		lastPageLoadWorld = { right: Number.NEGATIVE_INFINITY, bottom: Number.NEGATIVE_INFINITY };
		void loadRoots(true);
	}

	function setStarredOnly(value: boolean): void {
		studio.imagesStarredOnly = value;
		if (starredOnly === value) return;
		gridNavigationAnchor = null;
		gridScrollAnchor = captureGridScrollAnchor(imageFilters, gridFilter, value);
		rootsFilterModeEpoch += 1;
		starredOnly = value;
		pendingGridResumeAnchor = null;
		gridMiddleWindow = false;
		persistImagesPreferences();
		if (imagesMode === 'lineage') reloadRoots();
		else cancelRootsRequest();
	}

	function setGridFilter(value: GridFilter): void {
		if (gridFilter === value) return;
		gridNavigationAnchor = null;
		gridScrollAnchor = captureGridScrollAnchor(imageFilters, value);
		gridFilter = value;
		pendingGridResumeAnchor = null;
		gridMiddleWindow = false;
		persistImagesPreferences();
	}

	$effect(() => {
		const requested = studio.imagesStarredOnly;
		untrack(() => setStarredOnly(requested));
	});

	function scheduleSearchQuery(value: string): void {
		if (searchDebounce !== undefined) clearTimeout(searchDebounce);
		searchDebounce = setTimeout(() => {
			const next = value.trim();
			if (searchQuery === next) return;
			gridNavigationAnchor = null;
			searchQuery = next;
			if (imageFilterKey === null) return;
			pendingGridResumeAnchor = null;
			gridMiddleWindow = false;
			persistImagesPreferences();
		}, 200);
	}

	$effect(() => {
		completedHistorySignature;
		const q = searchQuery;
		const filterStarred = starredOnly;
		const filters = imageFilters;
		const referenceDate = imageFilterReferenceDate;
		if (imageFilterKey === null) return;
		const criteriaKey = JSON.stringify({ q, starred: filterStarred, imageFilterKey });
		const criteriaChanged = criteriaKey !== searchCriteriaKey;
		searchCriteriaKey = criteriaKey;
		const epoch = ++searchRequestEpoch;
		if (criteriaChanged) {
			searchMatchIds = null;
			searchMatchIndex = -1;
		}
		if (q === '') {
			searchMatchIds = null;
			searchMatchIndex = -1;
			return;
		}
		void (async () => {
			const search = new URLSearchParams({
				fields: 'ids',
				limit: '5000'
			});
			search.set('q', q);
			if (filterStarred) search.set('starred', 'true');
			const filteredSearch = withImageFilters(search, filters, referenceDate);
			if (filteredSearch === null) {
				if (epoch === searchRequestEpoch) searchMatchIds = new Set();
				return;
			}
			filteredSearch.append('state', 'succeeded');
			try {
				const body = await fetchCanvasJson<{ ids: string[] }>(
					`/api/v1/generations?${filteredSearch.toString()}`,
					'search request failed'
				);
				if (epoch !== searchRequestEpoch) return;
				searchMatchIds = new Set(body.ids);
				searchMatchIndex = -1;
			} catch (error) {
				if (error instanceof DOMException && error.name === 'AbortError') return;
				if (epoch !== searchRequestEpoch) return;
				if (criteriaChanged) searchMatchIds = new Set();
			}
		})();
	});

	function goToNextSearchMatch(): void {
		if (searchMatchIds === null || searchMatchIds.size === 0) return;
		const hits = lineageSearchMatchPositions(packedTrees, searchMatchIds);
		if (hits.length === 0) return;
		searchMatchIndex = nextLineageMatchIndex(searchMatchIndex, hits.length);
		const hit = hits[searchMatchIndex];
		if (hit === undefined) return;
		stopInertia();
		translateX = clampLineageCoordinate(viewportWidth / 2 - hit.x * scale);
		translateY = clampLineageCoordinate(viewportHeight / 2 - hit.y * scale);
	}

	async function toggleSelectedStar(): Promise<void> {
		if (selectedGeneration === null) return;
		const generation = selectedGeneration;
		await toggleStarWithUndo(generation.id, () => toggleGenerationStar(generation));
	}

	// The Undo offer calls this again after the selection may have moved on,
	// so it takes the generation rather than reading the selection.
	async function toggleGenerationStar(generation: Generation): Promise<boolean> {
		const changesRootFilter = generation.source_asset_id === null;
		if (changesRootFilter) {
			rootStarReconciliation = {
				...rootStarReconciliation,
				pending: rootStarReconciliation.pending + 1
			};
		}
		let succeeded = false;
		try {
			succeeded = await toggleStarred(generation.id);
			if (succeeded && starredOnly && !isStarred(generation.id)) {
				gridServerRows = gridServerRows.filter((row) => row.id !== generation.id);
				if (gridServerCursor === generation.id) {
					gridServerCursor = gridServerRows.at(-1)?.id ?? null;
				}
				if (gridSelectedId === generation.id) {
					gridSelectedId = null;
					requestedGridSelectionId = null;
					persistImagesPreferences();
				}
			} else if (succeeded && starredOnly && isStarred(generation.id)) {
				// The earlier Unstar removed this row from the committed server page.
				// Re-star from Undo must restore authoritative page order and membership.
				reloadGrid();
			}
		} finally {
			if (changesRootFilter) {
				const decision = settleLineageRootStarReconciliation(
					rootStarReconciliation,
					succeeded,
					starredOnly
				);
				rootStarReconciliation = decision.state;
				if (decision.reload) reloadRoots(true);
			}
		}
		return succeeded;
	}

	// Arrival markers have to come back off again. Tiles unmount once they leave
	// the viewport, so an id left in the set replays its entry animation every
	// time that tile is panned back into view.
	function markArrived(ids: string[]): void {
		if (viewOwnerEpoch() !== componentOwnerEpoch || reducedMotion || ids.length === 0) return;
		newNodeIds = new Set([...newNodeIds, ...ids]);
		setTimeout(() => {
			newNodeIds = new Set([...newNodeIds].filter((id) => !ids.includes(id)));
		}, 240);
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
			forest.ensureVisible(root, tree.layout);
		}
		const reachedRight =
			worldRect.right >= forestRight - 320 && worldRect.right >= lastPageLoadWorld.right + 320;
		const reachedBottom =
			worldRect.bottom >= forestBottom - 320 && worldRect.bottom >= lastPageLoadWorld.bottom + 320;
		if (
			rootsHaveMore &&
			!rootsFailed &&
			rootsCommittedFilterModeEpoch === rootsFilterModeEpoch &&
			(reachedRight || reachedBottom)
		) {
			lastPageLoadWorld = { right: worldRect.right, bottom: worldRect.bottom };
			void loadRoots();
		}
	});

	$effect(() => {
		const finished = studio.history.filter(
			(generation) =>
				generation.assets.length > 0 &&
				(!imageFiltersActive(imageFilters) ||
					generationMatchesImageFilters(generation, imageFilters, imageFilterReferenceDate))
		);
		roots = forest.reconcileFinished(roots, finished, starredOnly, isStarred);
	});

	$effect(() => {
		forest.reconcileHistory(persistedRoots, studio.history);
	});

	$effect(() => {
		if (
			!rootsInitialized ||
			rootsLoading ||
			rootsHaveMore ||
			rootsFailed ||
			rootsCommittedFilterModeEpoch !== rootsFilterModeEpoch
		)
			return;
		const rootIds = new Set(persistedRoots.map((root) => root.id));
		const bounded = retainedLineageTreeOffsets(
			treeOffsets,
			rootIds,
			starredOnly || imageFiltersActive(imageFilters)
		);
		if (Object.keys(bounded).length !== Object.keys(treeOffsets).length) {
			treeOffsets = bounded;
			saveTreeOffsetsForOwner(bounded);
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
		const id = focusedNodeId;
		const present = visibleNodes.some((item) => item.node.id === id);
		if (id === null || !present) return;
		untrack(() => {
			void tick().then(() => {
				const active = document.activeElement;
				const kind = lineageFocusActiveKind({
					focusedId: id,
					activeIsNull: active === null || active === document.body,
					activeIsViewport: active === viewportEl,
					activeIsInspector: active !== null && Boolean(inspectorEl?.contains(active)),
					activeNodeId: active instanceof HTMLElement ? (active.dataset.lineageNode ?? null) : null
				});
				if (!shouldRestoreLineageTileFocus(id, true, kind)) return;
				const tile = viewportEl?.querySelector(`[data-lineage-node="${CSS.escape(id)}"]`);
				if (tile instanceof HTMLElement) tile.focus();
			});
		});
	});

	$effect(() => {
		const viewport = lineageViewport();
		if (!viewportReady) return;
		if (viewportSaveTimer) clearTimeout(viewportSaveTimer);
		viewportSaveTimer = setTimeout(() => saveViewportForOwner(viewport), VIEWPORT_SAVE_DELAY);
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
		if (event.target instanceof Element && event.target.closest('button, [data-lineage-search]'))
			return;
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
				saveTreeOffsetsForOwner(treeOffsets);
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
		saveTreeOffsetsForOwner(treeOffsets);
	}

	function resetTreePosition(rootId: string): void {
		const { [rootId]: removed, ...remaining } = treeOffsets;
		void removed;
		treeOffsets = remaining;
		saveTreeOffsetsForOwner(treeOffsets);
	}

	function resetAllTreePositions(): void {
		treeOffsets = {};
		saveTreeOffsetsForOwner(treeOffsets);
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
			shouldSpendAnchorSearchPage({
				viewportReady,
				rootsHaveMore,
				rootsFailed,
				rootsLoading,
				pagesUsed: anchorSearchPages,
				maxPages: MAX_ANCHOR_SEARCH_PAGES,
				missingAnchor: [restoredViewport?.rootId, pendingLineageSelection?.rootId].some(
					(rootId) =>
						rootId !== null &&
						rootId !== undefined &&
						!persistedRoots.some((root) => root.id === rootId)
				)
			})
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

	function focusNode(id: string): void {
		focusedNodeId = id;
		const world = worldNodes.find((item) => item.node.id === id);
		if (
			world &&
			shouldPanToFocusedNode({
				nodeX: world.x,
				nodeY: world.y,
				tileWidth: LINEAGE_TILE_WIDTH,
				tileHeight: LINEAGE_TILE_HEIGHT,
				viewportWidth,
				viewportHeight,
				translateX,
				translateY,
				scale
			})
		) {
			stopInertia();
			const next = translationToCenterNode(world.x, world.y, viewportWidth, viewportHeight, scale);
			translateX = next.translateX;
			translateY = next.translateY;
		}
		void tick().then(() => {
			const tile = viewportEl?.querySelector(`[data-lineage-node="${CSS.escape(id)}"]`);
			if (tile instanceof HTMLElement) tile.focus();
		});
	}

	function onKeyDown(event: KeyboardEvent): void {
		if ((event.target as Element).closest('.lineage-tile, [data-lineage-search]')) return;
		if (event.key === 'Enter' && event.target === viewportEl) {
			const id = lineageRoverEntryId(lineageRover);
			if (id === null) return;
			event.preventDefault();
			focusNode(id);
			return;
		}
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

	function generationParams(data: CanvasNodeData): Record<string, unknown> {
		return { ...(data.generation?.params ?? {}) };
	}

	function edgeDelta(edge: {
		id: string;
		source: { data: CanvasNodeData };
		target: { data: CanvasNodeData };
	}): {
		prompt: ReturnType<typeof collapsePromptDiff>;
		params: ReturnType<typeof paramDeltas>;
		parentModel: string | null;
		childModel: string | null;
	} {
		const parent = generationParams(edge.source.data);
		const child = generationParams(edge.target.data);
		const full = promptWordDiff(String(parent.prompt ?? ''), String(child.prompt ?? ''));
		return {
			prompt: expandedEdgeIds.has(edge.id)
				? { tokens: full, truncated: false }
				: collapsePromptDiff(full),
			params: paramDeltas(parent, child),
			parentModel: edge.source.data.generation?.model_id ?? edge.source.data.entry.model_id,
			childModel: edge.target.data.generation?.model_id ?? edge.target.data.entry.model_id
		};
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

	const timeFormatter = $derived(
		new Intl.DateTimeFormat(getLocale(), {
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		})
	);

	function timeLabel(createdAt: string): string {
		return timeFormatter.format(new Date(createdAt));
	}

	function paramValue(value: unknown): string {
		if (typeof value === 'string') return value;
		// A strength of 0.7 that has been through one arithmetic step arrives
		// as 0.7000000000000001, and String() prints every digit of it. Twelve
		// significant figures is far more than any parameter here carries and
		// still collapses the binary-float tail.
		if (typeof value === 'number' && Number.isFinite(value)) {
			return String(Number(value.toPrecision(12)));
		}
		if (value === null || typeof value !== 'object') return String(value);
		return JSON.stringify(value);
	}

	async function selectNode(
		data: CanvasNodeData,
		rootId: string,
		origin: HTMLButtonElement
	): Promise<void> {
		selectionOrigin = origin;
		studio.lineageSelectedAssetId = data.entry.asset_id;
		pendingLineageSelection = { rootId, assetId: data.entry.asset_id };
		persistImagesPreferences();
		if (data.generation && generationById(data.generation.id) === undefined) {
			studio.selectedExtra = data.generation;
		}
		if (data.entry.job_id !== null) {
			void selectGeneration(data.entry.job_id);
		} else {
			studio.selectedId = null;
		}
		inspectorOpened();
		await tick();
		if (viewOwnerEpoch() !== componentOwnerEpoch) return;
		// preventScroll: the overlay slides in from off screen, and focusing it
		// mid-slide would scroll the clipped studio column sideways to reach it.
		inspectorEl?.focus({ preventScroll: true });
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
		void selectNode(data, rootId, event.currentTarget);
	}

	// Grid selection moves focus into the inspector, as lineage selection does,
	// so keyboard users do not keep tabbing through tiles behind the overlay.
	async function selectGridTile(id: string, origin: HTMLButtonElement): Promise<void> {
		gridSelectedId = id;
		requestedGridSelectionId = id;
		persistImagesPreferences();
		selectionOrigin = origin;
		inspectorOpened();
		await tick();
		if (viewOwnerEpoch() !== componentOwnerEpoch) return;
		// preventScroll: the overlay slides in from off screen, and focusing it
		// mid-slide would scroll the clipped studio column sideways to reach it.
		inspectorEl?.focus({ preventScroll: true });
	}

	// On a narrow screen the inspector is an overlay that slides in from the
	// end edge with a CSS transition, slides back out before it unmounts, and
	// can be swiped away. A new selection while it is sliding out reverses
	// the slide from wherever it is.
	const narrowScreen = new MediaQuery('max-width: 47.99rem', false);
	let inspectorLeaving = $state(false);
	let leaveTimer: ReturnType<typeof setTimeout> | null = null;

	function stopLeaving(): void {
		if (leaveTimer !== null) clearTimeout(leaveTimer);
		leaveTimer = null;
		inspectorLeaving = false;
	}

	/** A selection opened (or kept open) the inspector: give it a history
	 * entry for Back, and cancel a slide out in progress. */
	function inspectorOpened(): void {
		stopLeaving();
		pushOverlay('inspector');
	}

	const inspectorIsOpen = () =>
		(imagesMode === 'grid' ? gridSelectedId : studio.lineageSelectedAssetId) !== null &&
		!inspectorLeaving;

	/** Close the inspector from a button, Escape or a swipe: drop its
	 * history entry too. */
	function closeInspector(): void {
		if (!inspectorIsOpen()) return;
		void popOverlay('inspector');
		dismissInspector();
	}

	/** Close the inspector without touching the history: Back or Forward
	 * has already moved off its entry, and stepping back again would skip
	 * one. */
	function dismissInspector(): void {
		if (!inspectorIsOpen()) return;
		const panel = inspectorEl;
		if (!narrowScreen.current || reducedMotion || panel === null) {
			clearInspectorSelection();
			return;
		}
		inspectorLeaving = true;
		const finish = () => {
			panel.removeEventListener('transitionend', onEnd);
			if (!inspectorLeaving) return;
			stopLeaving();
			clearInspectorSelection();
		};
		const onEnd = (event: TransitionEvent) => {
			if (event.target === panel && event.propertyName === 'translate') finish();
		};
		panel.addEventListener('transitionend', onEnd);
		leaveTimer = setTimeout(finish, 400);
	}

	$effect(() => closeOnHistoryStep('inspector', inspectorIsOpen, dismissInspector));

	// An inspector entry with no inspector (the selection went away some
	// other way, or Back returned to a grid whose selection did not survive
	// the view) is cleared in place: a Back that changes nothing is a dead step.
	$effect(() => {
		const selected = imagesMode === 'grid' ? gridSelectedId : studio.lineageSelectedAssetId;
		if (selected === null && !inspectorLeaving && page.state.studioOverlay === 'inspector') {
			untrack(() => forgetOverlay('inspector'));
		}
	});

	// Swipe toward the end edge closes the overlay inspector.
	$effect(() => {
		const panel = inspectorEl;
		if (panel === null) return;
		return attachSwipeDismiss(panel, {
			direction: 1,
			enabled: () => narrowScreen.current,
			onDismiss: closeInspector
		});
	});

	function clearInspectorSelection(): void {
		if (imagesMode === 'grid') {
			if (gridSelectedId === null) return;
			gridSelectedId = null;
			requestedGridSelectionId = null;
			persistImagesPreferences();
			const origin = selectionOrigin?.isConnected ? selectionOrigin : null;
			selectionOrigin = null;
			void tick().then(() =>
				(origin ?? sectionEl?.querySelector<HTMLElement>('.grid-tile, .filter-pill'))?.focus()
			);
			return;
		}
		if (studio.lineageSelectedAssetId === null) return;
		studio.lineageSelectedAssetId = null;
		pendingLineageSelection = null;
		persistImagesPreferences();
		studio.selectedId = null;
		const target = selectionOrigin?.isConnected ? selectionOrigin : viewportEl;
		selectionOrigin = null;
		void tick().then(() => target?.focus());
	}

	function modalIsOpen(): boolean {
		return document.querySelector('[role="dialog"][aria-modal="true"], dialog[open]') !== null;
	}

	function onWindowKeyDown(event: KeyboardEvent): void {
		if (
			event.key !== 'Escape' ||
			(imagesMode === 'grid' ? gridSelectedId : studio.lineageSelectedAssetId) === null
		)
			return;
		if (modalIsOpen() || event.defaultPrevented) return;
		const target = event.target as HTMLElement | null;
		if (target?.closest('input, textarea, select')) return;
		if (!sectionEl?.contains(document.activeElement)) return;
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
		saveViewportForOwner(lineageViewport());
	}

	function openFromNode(
		data: CanvasNodeData,
		mode: 'generate' | 'image_to_image' | 'upscale'
	): void {
		const generation =
			data.generation ?? (data.entry.job_id ? (generationById(data.entry.job_id) ?? null) : null);
		const asset =
			generation?.assets.find((item) => item.id === data.entry.asset_id) ??
			generation?.assets[0] ??
			null;
		const prompt = (generation?.params.prompt ?? '').trim();
		const modelId = generation?.model_id ?? data.entry.model_id;
		const params = { ...(generation?.params ?? {}) };
		delete params.seed;
		if (mode === 'generate') {
			if (prompt === '' || textToImageModels.length === 0) return;
			studio.modelId = preferredModelId(textToImageModels, modelId);
			studio.generationPrefill = {
				mode,
				sourceAssetId: null,
				prompt,
				modelId: studio.modelId,
				params: {}
			};
		} else if (mode === 'image_to_image') {
			if (asset === null || data.entry.missing || imageToImageModels.length === 0) return;
			studio.imageToImageModelId = preferredModelId(imageToImageModels, modelId);
			studio.generationPrefill = {
				mode,
				sourceAssetId: asset.id,
				prompt,
				modelId: studio.imageToImageModelId,
				params
			};
		} else {
			if (asset === null || data.entry.missing || upscaleModels.length === 0) return;
			studio.upscaleModelId =
				upscaleModels.find((model) => model.id === modelId)?.id ??
				defaultUpscaleModelId(studio.models);
			studio.generationPrefill = {
				mode,
				sourceAssetId: asset.id,
				prompt,
				modelId: studio.upscaleModelId,
				params
			};
		}
		studio.prompt = prompt;
		saveViewport();
		openService(mode);
	}

	function openFromSelection(mode: 'generate' | 'image_to_image' | 'upscale'): void {
		if (selectedData === null) return;
		openFromNode(selectedData, mode);
	}

	function branchFromNode(data: CanvasNodeData): void {
		if (!mayCreate) return;
		const generation =
			data.generation ?? (data.entry.job_id ? (generationById(data.entry.job_id) ?? null) : null);
		const asset =
			generation?.assets.find((item) => item.id === data.entry.asset_id) ??
			generation?.assets[0] ??
			null;
		const prompt = (generation?.params.prompt ?? '').trim();
		const hasBytes = !data.entry.missing && asset !== null;
		if (hasBytes && imageToImageModels.length > 0) {
			openFromNode(data, 'image_to_image');
			return;
		}
		if (prompt !== '' && textToImageModels.length > 0) {
			openFromNode(data, 'generate');
			return;
		}
		if (hasBytes && upscaleModels.length > 0) openFromNode(data, 'upscale');
	}

	function imageUrl(data: CanvasNodeData, band: typeof lod): string | null {
		if (data.entry.missing || failedImageIds.has(data.entry.asset_id)) return null;
		if (band === 'cards') {
			return data.generation?.assets[0]?.url ?? data.entry.thumbnail_url;
		}
		return data.entry.thumbnail_url;
	}

	function replaceGeneration(assetId: string, generation: Generation): void {
		roots = forest.replaceGeneration(roots, assetId, generation);
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
		if (imagesMode === 'lineage') void loadRoots(true);
		void (async () => {
			try {
				const response = await fetch('/api/v1/generations/models');
				if (!response.ok) throw new Error('model options request failed');
				const body = (await response.json()) as { models: ImageModelOption[] };
				imageModelOptions = body.models;
			} catch {
				imageModelsFailed = true;
			} finally {
				imageModelsLoading = false;
			}
		})();
	});

	// The lineage viewport only exists in Lineage mode, so observe it whenever it
	// mounts, including after switching from Grid, not just on first mount.
	$effect(() => {
		const el = viewportEl;
		if (!el) return;
		const resize = new ResizeObserver(([entry]) => {
			viewportWidth = entry.contentRect.width;
			viewportHeight = entry.contentRect.height;
			if (!viewportReady && rootsInitialized && viewportWidth > 0 && viewportHeight > 0) {
				initializeViewport();
			}
		});
		resize.observe(el);
		return () => resize.disconnect();
	});

	onDestroy(() => {
		if (gridAnchorSaveTimer !== undefined) clearTimeout(gridAnchorSaveTimer);
		const anchor = captureGridAnchorBeforeExit();
		if (anchor !== null) pendingGridResumeAnchor = anchor;
		persistImagesPreferences();
		if (viewportReady) saveViewportForOwner(lineageViewport());
		canvasActive = false;
		for (const controller of requestControllers) controller.abort();
		requestControllers.clear();
		forest.stop();
		stopInertia();
		if (initializeFrame) cancelAnimationFrame(initializeFrame);
		if (recenterTimer) clearTimeout(recenterTimer);
		if (viewportSaveTimer) clearTimeout(viewportSaveTimer);
		if (searchDebounce !== undefined) clearTimeout(searchDebounce);
		gridAnchorSaveTimer = undefined;
		// A slide out cut short by unmounting still ends the selection it
		// was closing, now rather than from a timer that outlives the view.
		const wasLeaving = inspectorLeaving;
		stopLeaving();
		if (wasLeaving) clearInspectorSelection();
	});
</script>

<svelte:window onkeydown={onWindowKeyDown} />

{#snippet imagesHeader()}
	<div bind:this={imagesHeaderEl} class="contents" data-images-header>
		<div class="flex items-center gap-1.5" data-lineage-search>
			<label
				class="bg-field border-line-strong focus-within:ring-ring/60 flex h-[30px] w-36 min-w-0 items-center gap-2 rounded-md border px-2.5 focus-within:ring-2 sm:w-64"
			>
				<SearchIcon class="text-dim size-3.5 shrink-0" aria-hidden="true" />
				<input
					type="search"
					name="q"
					autocomplete="off"
					spellcheck={false}
					class="placeholder:text-dim min-w-0 flex-1 bg-transparent text-[13px] outline-none"
					bind:value={searchDraft}
					placeholder={t('app.images.search')}
					aria-label={t('app.images.search')}
					oninput={() => scheduleSearchQuery(searchDraft)}
				/>
			</label>
			{#if imagesMode === 'lineage'}
				<Button
					variant="outline"
					size="sm"
					class="hidden lg:inline-flex"
					disabled={searchMatchIds === null || searchMatchIds.size === 0}
					onclick={goToNextSearchMatch}
				>
					{t('app.images.next_match')}
				</Button>
			{/if}
		</div>
		<div
			class="bg-field border-line-strong flex gap-0.5 rounded-[7px] border p-0.5"
			role="group"
			aria-label={t('app.images.layout')}
		>
			{#each [{ mode: 'grid', label: t('app.images.mode_grid'), icon: LayoutGridIcon }, { mode: 'lineage', label: t('app.images.mode_lineage'), icon: GitForkIcon }] as const as option (option.mode)}
				<button
					type="button"
					class={'flex h-[26px] min-w-24 items-center justify-center gap-1.5 rounded-[5px] px-3 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
						(imagesMode === option.mode
							? 'bg-hover text-foreground ring-line-strong shadow-[0_1px_2px_rgba(0,0,0,0.4)] ring-1 ring-inset'
							: 'text-muted-foreground hover:text-foreground')}
					aria-pressed={imagesMode === option.mode}
					onclick={() => setImagesMode(option.mode)}
				>
					<option.icon class="size-3.5" aria-hidden="true" />
					{option.label}
				</button>
			{/each}
		</div>
		{#if imagesMode === 'grid' && gridMiddleWindow}
			<Button
				variant="outline"
				size="sm"
				disabled={gridLoading}
				aria-label={t('app.images.newest')}
				onclick={loadNewestGridPage}
			>
				{t('app.images.newest')}
			</Button>
		{/if}
	</div>
{/snippet}

<div
	bind:this={sectionEl}
	class="lineage-section relative grid h-full min-h-0 grid-cols-[minmax(0,1fr)_auto] grid-rows-[auto_minmax(0,1fr)]"
>
	<header class="sr-only">
		<h1>{t('app.images.title')}</h1>
		<p>{t('app.images.sub')}</p>
	</header>
	<div class="col-start-1 row-start-1 flex flex-wrap items-center gap-2 px-4 pt-3 pb-3">
		<div class="flex flex-wrap gap-1.5" role="group" aria-label={t('app.images.filters')}>
			<button
				type="button"
				class={'filter-pill ' + (allFilterOn ? 'is-on' : '')}
				aria-pressed={allFilterOn}
				onclick={() => {
					setGridFilter('all');
					setStarredOnly(false);
					setImageFilters({ ...defaultImageFilters, models: [] });
				}}>{t('app.models.filter_all')}</button
			>
			<button
				type="button"
				class={'filter-pill ' + (starredOnly ? 'is-on' : '')}
				aria-pressed={starredOnly}
				onclick={() => setStarredOnly(!starredOnly)}>{t('app.shell.starred')}</button
			>
			{#if imagesMode === 'grid'}
				{#each ['generate', 'image_to_image', 'upscale'] as const as action (action)}
					<button
						type="button"
						class={'filter-pill ' + (gridFilter === action ? 'is-on' : '')}
						aria-pressed={gridFilter === action}
						onclick={() => {
							const next = gridFilter === action ? 'all' : action;
							setGridFilter(next);
						}}>{actionLabel(action)}</button
					>
				{/each}
			{/if}
		</div>
		<ImageFilterControls
			filters={imageFilters}
			options={imageModelOptions}
			loading={imageModelsLoading}
			failed={imageModelsFailed}
			onchange={setImageFilters}
		/>
		{#if imagesMode === 'lineage' && imageFiltersActive(imageFilters)}
			<span class="text-dim text-xs">{t('app.images.lineage_context')}</span>
		{/if}
		<span class="flex-1"></span>
		{#if imagesMode === 'grid'}
			<span class="text-dim text-xs tabular-nums">
				{t('app.images.grid_count').replace('{count}', String(gridItems.length))}
			</span>
		{/if}
	</div>
	<p class="sr-only" role="status">
		{#if rootsLoading && roots.length === 0}
			{t('app.images.loading')}
		{:else if rootsFailed}
			{t('app.images.load_failed')}
		{:else if persistedRoots.length === 0}
			{#if imageFiltersActive(imageFilters)}
				{t('app.images.no_filter_matches')}
			{:else if starredOnly}
				{t('app.images.no_starred_roots')}
			{:else}
				{t('app.gen.result_hint')}
			{/if}
		{:else if searchQuery !== ''}
			{t('app.images.matches').replace('{count}', String(searchMatchIds?.size ?? 0))}
		{/if}
	</p>
	{#if imagesMode === 'grid'}
		<div
			bind:this={gridScrollerEl}
			{@attach attachGridDragScroll}
			onscroll={saveGridAnchorSoon}
			class="grid-scroller col-start-1 row-start-2 min-h-0 cursor-grab overflow-y-auto px-4 pb-4"
			aria-busy={gridLoading}
			role="region"
			aria-label={t('app.images.mode_grid')}
		>
			{#if gridLoading && gridItems.length === 0}
				<div role="status">
					<span class="sr-only">{t('app.images.loading')}</span>
					<ul class="image-grid" aria-hidden="true">
						{#each Array.from({ length: 12 }, (_, index) => index) as index (index)}
							<li><Skeleton class="aspect-square w-full rounded-lg" /></li>
						{/each}
					</ul>
				</div>
			{:else if gridFailed && gridItems.length === 0}
				<p class="text-muted-foreground grid h-40 place-items-center text-[13px]" role="alert">
					{t('app.images.load_failed')}
				</p>
			{:else if gridItems.length === 0 && !gridLoading}
				<p class="text-muted-foreground grid h-40 place-items-center text-[13px]">
					{starredOnly
						? t('app.images.no_starred_roots')
						: imageFiltersActive(imageFilters)
							? t('app.images.no_filter_matches')
							: t('app.gen.result_hint')}
				</p>
			{:else if gridItems.length > 0}
				<ul class="image-grid" aria-label={t('app.images.title')} aria-busy={gridLoading}>
					{#each gridItems as generation (generation.id)}
						{@const tileLabel =
							generation.params.prompt?.trim() || actionLabel(generationAction(generation))}
						<li>
							<button
								type="button"
								class={'grid-tile shimmer ' +
									(gridSelectedId === generation.id ? 'is-selected' : '')}
								data-generation-id={generation.id}
								aria-pressed={gridSelectedId === generation.id}
								title={tileLabel}
								onclick={(event) => void selectGridTile(generation.id, event.currentTarget)}
							>
								<img
									src={generation.assets[0].thumbnail_url ?? generation.assets[0].url}
									alt={tileLabel}
									loading="lazy"
									decoding="async"
									draggable="false"
									class="img-reveal"
									{@attach revealOnLoad(
										generation.assets[0].thumbnail_url ?? generation.assets[0].url
									)}
								/>
								{#if isStarred(generation.id)}
									<span class="grid-star" aria-hidden="true"><StarIcon /></span>
								{/if}
								{#if typeof generation.params.factor === 'number'}
									<span class="grid-factor" aria-hidden="true">×{generation.params.factor}</span>
								{/if}
							</button>
						</li>
					{/each}
				</ul>
			{/if}
			{#if gridFailed && gridItems.length > 0}
				<p class="text-muted-foreground mt-4 text-center text-[13px]" role="alert">
					{t('app.images.load_failed')}
				</p>
			{/if}
			{#if gridHasMore || gridFailed}
				<div class="mt-4 flex justify-center">
					<Button
						variant="outline"
						size="sm"
						disabled={gridLoading || imageFilterKey === null}
						onclick={() => void loadGridPage(gridFailed ? gridFailedAppend : gridHasMore)}
					>
						{gridLoading
							? t('app.images.loading')
							: gridFailed
								? t('app.images.retry')
								: t('app.images.more_matches')}
					</Button>
				</div>
			{/if}
		</div>
	{:else}
		<!-- A focusable canvas region owns the documented pan and zoom keyboard controls. -->
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<div
			bind:this={viewportEl}
			{@attach canvasInteractions}
			class="lineage-viewport border-border bg-card/20 relative col-start-1 row-start-2 mx-4 mb-4 min-h-0 min-w-0 overflow-hidden rounded-[10px] border"
			class:is-panning={panPointerId !== null || pinch !== null || dragPointerId !== null}
			role="application"
			aria-label={t('app.images.canvas')}
			tabindex="0"
		>
			<div class="absolute end-3 top-3 z-30 flex flex-wrap items-center justify-end gap-1">
				<Button
					variant="secondary"
					size="icon-sm"
					class="hit-slop relative [--hit-slop-x:2px] [--hit-slop-y:2px]"
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
					class="hit-slop relative [--hit-slop-x:2px] [--hit-slop-y:2px]"
					title={t('app.images.zoom_out')}
					aria-label={t('app.images.zoom_out')}
					onclick={() => zoomAt(scale / 1.2, viewportWidth / 2, viewportHeight / 2)}
				>
					<MinusIcon />
				</Button>
				<Button
					variant="secondary"
					size="icon-sm"
					class="hit-slop relative [--hit-slop-x:2px] [--hit-slop-y:2px]"
					title={t('app.images.zoom_in')}
					aria-label={t('app.images.zoom_in')}
					onclick={() => zoomAt(scale * 1.2, viewportWidth / 2, viewportHeight / 2)}
				>
					<PlusIcon />
				</Button>
				<Button
					variant="secondary"
					size="icon-sm"
					class="hit-slop relative [--hit-slop-x:2px] [--hit-slop-y:2px]"
					title={t('app.images.recenter')}
					aria-label={t('app.images.recenter')}
					onclick={() => recenterNewest()}
				>
					<LocateFixedIcon />
				</Button>
			</div>

			{#if rootsLoading && roots.length === 0}
				<!-- A tree's outline while the roots load; the status line above
				     the canvas announces it. -->
				<div class="absolute inset-0 grid place-items-center" aria-hidden="true">
					<div class="flex flex-col items-center gap-6">
						<Skeleton class="size-24 rounded-lg" />
						<div class="flex gap-6">
							<Skeleton class="size-[72px] rounded-lg" />
							<Skeleton class="size-[72px] rounded-lg" />
							<Skeleton class="size-[72px] rounded-lg" />
						</div>
					</div>
				</div>
			{:else if rootsFailed}
				<div class="absolute inset-0 z-20 grid place-items-center text-sm">
					<div class="flex flex-col items-center gap-3">
						<span class="text-muted-foreground">{t('app.images.load_failed')}</span>
						<Button
							variant="secondary"
							size="sm"
							onclick={() => void loadRoots(!rootsFailedAppend)}
						>
							{t('app.images.retry')}
						</Button>
					</div>
				</div>
			{:else if persistedRoots.length === 0}
				<div class="text-muted-foreground absolute inset-0 grid place-items-center text-sm">
					{#if imageFiltersActive(imageFilters)}
						{t('app.images.no_filter_matches')}
					{:else if starredOnly}
						{t('app.images.no_starred_roots')}
					{:else}
						{t('app.gen.result_hint')}
					{/if}
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
									class:is-dimmed={(selectedNode !== null && !selectedPathEdgeIds.has(edge.id)) ||
										(searchMatchIds !== null && !searchMatchRootIds.has(tree.rootId))}
								>
									<path
										class="lineage-edge"
										d={lineageEdgePath(edge.source, edge.target, tree.x, tree.y)}
										marker-end="url(#lineage-arrow)"
									/>
									{#if lod !== 'cards'}
										<text
											x={tree.x + (edge.source.x + edge.target.x) / 2}
											y={tree.y + (edge.source.y + edge.target.y) / 2 - 7}
										>
											{actionLabel(edge.target.data.entry.action)}
										</text>
									{/if}
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

				{#if lod === 'cards'}
					{#each packedTrees as tree (tree.rootId)}
						{#each tree.layout.edges as edge (edge.id)}
							{@const edgeLeft = tree.x + Math.min(edge.source.x, edge.target.x)}
							{@const edgeTop = tree.y + Math.min(edge.source.y, edge.target.y)}
							{@const edgeRight = tree.x + Math.max(edge.source.x, edge.target.x)}
							{@const edgeBottom = tree.y + Math.max(edge.source.y, edge.target.y)}
							{#if rectsIntersect( worldRect, { left: edgeLeft, top: edgeTop, right: edgeRight, bottom: edgeBottom } )}
								{@const delta = edgeDelta(edge)}
								<dl
									class="edge-delta"
									class:is-active={selectedPathEdgeIds.has(edge.id)}
									class:is-dimmed={selectedNode !== null && !selectedPathEdgeIds.has(edge.id)}
									style={`left: ${tree.x + (edge.source.x + edge.target.x) / 2}px; top: ${tree.y + (edge.source.y + edge.target.y) / 2}px`}
								>
									<div>
										<dt>{t('app.images.delta_action')}</dt>
										<dd>{actionLabel(edge.target.data.entry.action)}</dd>
									</div>
									{#each delta.prompt.tokens as token, tokenIndex (`${token.kind}:${tokenIndex}`)}
										{#if token.kind === 'added' || token.kind === 'removed'}
											<div>
												<dt>
													{token.kind === 'added'
														? t('app.images.delta_added')
														: t('app.images.delta_removed')}
												</dt>
												<dd>{token.text}</dd>
											</div>
										{/if}
									{/each}
									{#if delta.prompt.truncated}
										<button
											type="button"
											class="edge-delta-expand"
											onclick={(event) => {
												event.stopPropagation();
												expandedEdgeIds = new Set([...expandedEdgeIds, edge.id]);
											}}
										>
											{t('app.images.delta_expand')}
										</button>
									{/if}
									{#if delta.parentModel && delta.childModel && delta.parentModel !== delta.childModel}
										<div>
											<dt>{t('app.images.delta_model')}</dt>
											<dd>{delta.parentModel} -> {delta.childModel}</dd>
										</div>
									{/if}
									{#each delta.params as change (change.kind === 'new-seed' ? 'new-seed' : `${change.kind}:${change.key}`)}
										<div>
											{#if change.kind === 'new-seed'}
												<dt>{t('app.images.delta_new_seed')}</dt>
												<dd></dd>
											{:else if change.kind === 'changed'}
												<dt>{change.key}</dt>
												<dd>{change.from} -> {change.to}</dd>
											{:else if change.kind === 'added'}
												<dt>{t('app.images.delta_added')}</dt>
												<dd>{change.key}: {change.value}</dd>
											{:else}
												<dt>{t('app.images.delta_removed')}</dt>
												<dd>{change.key}: {change.value}</dd>
											{/if}
										</div>
									{/each}
								</dl>
							{/if}
						{/each}
					{/each}
				{/if}

				{#each visibleNodes as item (`${item.rootId}:${item.node.id}`)}
					{@const data = item.node.data}
					{@const shownImage = imageUrl(data, lod)}
					{@const starred = data.entry.job_id !== null && isStarred(data.entry.job_id)}
					{@const searchTone = lineageSearchTone(data.entry.job_id, searchMatchIds)}
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
							class:is-selected={studio.lineageSelectedAssetId === data.entry.asset_id ||
								(data.entry.missing && studio.lineageSelectedAssetId === data.entry.job_id)}
							class:is-missing={data.entry.missing || shownImage === null}
							class:is-starred={starred}
							class:is-search-match={searchTone === 'match'}
							class:is-search-dim={searchTone === 'dim'}
							style={`--tile-pull: ${proximityScale(item)}`}
							aria-label={`${actionLabel(data.entry.action)}: ${promptLabel(data)}${starred ? `. ${t('app.images.starred')}` : ''}${item.isRoot ? `. ${t('app.images.drag_tree')}` : ''}${item.treeStatus === 'loading' ? `. ${t('app.images.tree_loading')}` : ''}${item.treeStatus === 'error' ? `. ${t('app.images.tree_load_failed')}` : ''}`}
							title={promptLabel(data)}
							tabindex={lineageTileTabIndex(item.node.id, focusedNodeId)}
							data-lineage-node={item.node.id}
							onfocus={() => (focusedNodeId = item.node.id)}
							onpointerdown={(event) => {
								if (item.isRoot) startTreeDrag(event, item.rootId);
							}}
							onkeydown={(event) => {
								if (event.key === 'Escape') {
									event.preventDefault();
									event.stopPropagation();
									viewportEl?.focus();
									return;
								}
								if (event.altKey && item.isRoot) {
									moveTreeFromKeyboard(event, item.rootId);
									return;
								}
								if (event.altKey) return;
								const next = nextLineageRoverId(lineageRover, item.node.id, event.key);
								if (next === null) {
									if (
										event.key === 'ArrowLeft' ||
										event.key === 'ArrowRight' ||
										event.key === 'ArrowUp' ||
										event.key === 'ArrowDown'
									) {
										event.preventDefault();
										event.stopPropagation();
									}
									return;
								}
								event.preventDefault();
								event.stopPropagation();
								focusNode(next);
							}}
							onclick={(event) => onTileClick(event, data, item.rootId, item.isRoot)}
							ondblclick={(event) => {
								event.preventDefault();
								event.stopPropagation();
								branchFromNode(data);
							}}
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
						{#if item.isRoot && item.treeStatus === 'error'}
							{@const root = persistedRoots.find((entry) => entry.id === item.rootId)}
							{#if root}
								<button
									type="button"
									class="retry-tree-load"
									title={t('app.images.retry_tree_load')}
									aria-label={t('app.images.retry_tree_load')}
									onpointerdown={(event) => event.stopPropagation()}
									onclick={(event) => {
										event.stopPropagation();
										forest.force(root);
									}}
								>
									<RefreshCwIcon />
								</button>
							{/if}
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
	{/if}

	{#if selectedData !== null}
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<aside
			bind:this={inspectorEl}
			data-leaving={inspectorLeaving ? '' : undefined}
			class="selection-inspector border-border bg-panel absolute inset-y-0 end-0 z-40 row-span-2 row-start-1 flex w-[min(22rem,calc(100%-1rem))] flex-col overflow-y-auto border-s p-4 shadow-xl md:relative md:inset-auto md:z-auto md:col-start-2 md:w-80 md:shadow-none xl:w-[320px]"
			aria-labelledby="selection-inspector-title"
			tabindex="-1"
		>
			<div class="mb-4 flex items-start justify-between gap-3">
				<div>
					<h2 id="selection-inspector-title" class="text-[13px] font-semibold">
						{t('app.images.inspector_title')}
					</h2>
					<p class="text-muted-foreground mt-1 text-xs">
						{actionLabel(selectedData.entry.action)}
					</p>
				</div>
				<Button
					variant="ghost"
					size="icon-sm"
					class="hit-slop relative"
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
					class="reveal-stack bg-raised ring-line-strong relative mb-3 block aspect-square shrink-0 overflow-hidden rounded-lg ring-1"
					title={t('app.gen.open_full')}
				>
					{#if selectedThumbnailUrl !== null && selectedThumbnailUrl !== selectedImageUrl}
						<img
							src={selectedThumbnailUrl}
							alt=""
							aria-hidden="true"
							class="img-placeholder absolute inset-0 h-full w-full object-contain [--placeholder-radius:0px]"
						/>
					{/if}
					<img
						src={selectedImageUrl}
						alt={selectedPrompt || t('app.gen.result')}
						class="img-reveal relative h-full w-full object-contain"
						{@attach revealOnLoad(selectedImageUrl)}
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

			<div class="mb-4 grid grid-cols-2 gap-2">
				<Button
					variant="outline"
					size="sm"
					disabled={selectedGeneration === null}
					onclick={() => void toggleSelectedStar()}
				>
					<StarIcon class={selectedIsStarred ? 'text-status-warning fill-current' : ''} />
					{selectedIsStarred ? t('app.gen.unstar') : t('app.gen.star')}
				</Button>
				<Button
					href={selectedAsset?.download_url}
					variant="outline"
					size="sm"
					disabled={!selectedHasBytes}
				>
					<DownloadIcon />
					{t('app.gen.download')}
				</Button>
				<Button
					variant="outline"
					size="sm"
					disabled={!canUpscaleSelected}
					onclick={() => openFromSelection('upscale')}
				>
					<ScanLineIcon />
					{t('app.images.action_upscale')}
				</Button>
				<Button
					variant="outline"
					size="sm"
					disabled={!canEditSelected}
					onclick={() => openFromSelection('image_to_image')}
				>
					<PencilIcon />
					{t('app.images.action_edit')}
				</Button>
				<Button
					variant="brand"
					size="sm"
					class="col-span-2"
					disabled={!canGenerateFromPrompt}
					onclick={() => openFromSelection('generate')}
				>
					<WandSparklesIcon />
					{t('app.images.action_generate_prompt')}
				</Button>
			</div>

			<dl class="grid gap-3 text-[12.5px]">
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
				{#if selectedGeneration?.gpu_ms != null}
					<div>
						<dt class="text-muted-foreground text-xs">{t('app.gen.gpu_time')}</dt>
						<dd class="mt-1 tabular-nums">{formatMs(selectedGeneration.gpu_ms)}</dd>
					</div>
				{/if}
				{#if selectedParams.length > 0}
					<div>
						<dt class="text-muted-foreground text-xs">{t('app.images.parameters')}</dt>
						<dd class="mt-2 grid grid-cols-2 gap-x-4 gap-y-2">
							{#each selectedParams as [key, value] (key)}
								<span class="text-muted-foreground">{key}</span>
								<span class="min-w-0 break-words text-end font-sans text-xs tabular-nums">
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
	.filter-pill {
		display: inline-flex;
		height: 26px;
		align-items: center;
		border: 1px solid var(--line-strong);
		border-radius: 999px;
		padding: 0 10px;
		color: var(--muted-foreground);
		font-size: 12px;
		font-weight: 500;
		outline: none;
	}
	.filter-pill:hover {
		color: var(--foreground);
	}
	.filter-pill.is-on {
		border-color: var(--line-strong);
		background: var(--hover);
		color: var(--foreground);
	}
	.filter-pill:focus-visible,
	.grid-tile:focus-visible {
		box-shadow: 0 0 0 2px var(--ring);
	}
	.image-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
		gap: 9px;
	}
	.grid-tile {
		position: relative;
		display: block;
		cursor: grab;
		width: 100%;
		aspect-ratio: 1;
		overflow: hidden;
		border-radius: 8px;
		box-shadow: 0 0 0 1px var(--line-strong);
		outline: none;
	}
	:global(.grid-scroller.is-drag-scrolling .grid-tile) {
		cursor: grabbing;
	}
	.is-drag-scrolling {
		cursor: grabbing;
		user-select: none;
	}
	.grid-tile img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.grid-tile.is-selected {
		box-shadow:
			0 0 0 2px var(--brand),
			0 0 0 1px var(--line-strong);
	}
	.grid-star {
		position: absolute;
		top: 6px;
		right: 6px;
		color: var(--status-warning);
	}
	.grid-star :global(svg) {
		width: 14px;
		height: 14px;
	}
	.grid-factor {
		position: absolute;
		bottom: 6px;
		left: 6px;
		border-radius: 4px;
		background: rgb(0 0 0 / 55%);
		padding: 0 5px;
		color: #ededed;
		font-size: 11px;
	}

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

	.edge-delta {
		position: absolute;
		z-index: 2;
		width: max-content;
		max-width: 14rem;
		margin: 0;
		padding: 0.35rem 0.5rem;
		transform: translate(-50%, -50%);
		background: color-mix(in oklab, var(--background) 88%, transparent);
		border: 1px solid var(--border);
		border-radius: 0.4rem;
		color: var(--foreground);
		font-size: 11px;
		line-height: 1.35;
		pointer-events: auto;
	}

	.edge-delta.is-dimmed {
		opacity: 0.16;
	}

	.edge-delta div {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 0.25rem 0.5rem;
	}

	.edge-delta dt {
		margin: 0;
		color: var(--muted-foreground);
		font-weight: 600;
	}

	.edge-delta dd {
		margin: 0;
		min-width: 0;
		overflow-wrap: anywhere;
	}

	.edge-delta-expand {
		margin-top: 0.25rem;
		padding: 0;
		border: 0;
		background: none;
		color: var(--primary);
		font: inherit;
		text-decoration: underline;
		cursor: pointer;
	}

	.edge-delta-expand:hover {
		background: var(--accent);
	}

	.edge-delta-expand:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
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

	.retry-tree-load {
		position: absolute;
		top: calc(50% - var(--visible-tile-half-height) - 10px);
		right: calc(50% + var(--visible-tile-half-width) - 10px);
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

	.retry-tree-load:hover {
		background: var(--accent);
	}

	.retry-tree-load:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.retry-tree-load :global(svg) {
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

	.lineage-tile.is-search-dim {
		opacity: 0.28;
	}

	.lineage-tile.is-search-match {
		outline: 2px solid var(--primary);
		outline-offset: 2px;
	}

	/* Below md the inspector overlays the canvas: it slides in from the end
	   edge and back out (data-leaving) with interruptible CSS transitions, and
	   pan-y leaves horizontal drags to the swipe that dismisses it. */
	@media (max-width: 47.99rem) {
		.selection-inspector {
			transition: translate 280ms cubic-bezier(0.32, 0.72, 0, 1);
			touch-action: pan-y;
		}
		.selection-inspector[data-leaving] {
			translate: 100% 0;
			pointer-events: none;
		}
		@starting-style {
			.selection-inspector {
				translate: 100% 0;
			}
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.selection-inspector {
			transition: none;
		}
	}

	.selection-inspector:focus:not(:focus-visible) {
		outline: none;
	}

	.selection-inspector:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
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

	.lod-constellation .lineage-tile.is-starred {
		border-color: var(--primary);
		box-shadow: 0 0 0 4px color-mix(in oklch, var(--primary) 78%, transparent);
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

	.lod-constellation .retry-tree-load {
		display: none;
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
