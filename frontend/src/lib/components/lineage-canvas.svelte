<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import ImageOffIcon from '@lucide/svelte/icons/image-off';
	import LoaderCircleIcon from '@lucide/svelte/icons/loader-circle';
	import LocateFixedIcon from '@lucide/svelte/icons/locate-fixed';
	import MinusIcon from '@lucide/svelte/icons/minus';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import { Button } from '$lib/components/ui/button';
	import { getLocale, t } from '$lib/i18n.svelte';
	import {
		LINEAGE_TILE_HEIGHT,
		LINEAGE_TILE_WIDTH,
		layoutLineageTree,
		lineageLod,
		packLineageForest,
		rectsIntersect,
		viewportWorldRect,
		type LineageLayoutNode,
		type LineageTreeLayout,
		type PackedLineageTree,
		type PositionedLineageNode
	} from '$lib/lineage-layout';
	import {
		selectGeneration,
		studio,
		type Generation,
		type GenerationLineage,
		type LineageEntry
	} from '$lib/studio.svelte';

	const ROOT_LIMIT = 50;
	const MAX_MOUNTED_TILES = 600;
	const MIN_SCALE = 0.12;
	const MAX_SCALE = 1.6;
	const PAN_STEP = 80;
	const HOVER_RADIUS = 150;
	const HOVER_PULL = 0.08;
	const INERTIA_MIN = 0.04;
	const INERTIA_FRICTION = 0.004;

	type CanvasNodeData = {
		entry: LineageEntry;
		generation: Generation | null;
	};

	type CachedTree = {
		status: 'loading' | 'loaded' | 'error';
		layout: LineageTreeLayout<CanvasNodeData> | null;
	};

	type VisibleNode = {
		rootId: string;
		x: number;
		y: number;
		node: PositionedLineageNode<CanvasNodeData>;
	};

	type PointerSample = { x: number; y: number; time: number };

	let viewportEl = $state<HTMLDivElement | null>(null);
	let viewportWidth = $state(0);
	let viewportHeight = $state(0);
	let translateX = $state(72);
	let translateY = $state(72);
	let scale = $state(1);
	let roots = $state<Generation[]>([]);
	let rootsLoading = $state(false);
	let rootsFailed = $state(false);
	let rootsHaveMore = $state(false);
	let treeCache = $state(new Map<string, CachedTree>());
	let newNodeIds = $state(new Set<string>());
	let failedImageIds = $state(new Set<string>());
	let refreshingImageIds = $state(new Set<string>());
	let refreshedImageIds = new Set<string>();
	let pointerWorld = $state<{ x: number; y: number } | null>(null);
	let focusedNodeId = $state<string | null>(null);
	let reducedMotion = false;
	let recentering = $state(false);
	let recenterTimer: ReturnType<typeof setTimeout> | null = null;
	let inertiaFrame = 0;
	let panPointerId = $state<number | null>(null);
	let panStart = { x: 0, y: 0, translateX: 0, translateY: 0 };
	let lastPanSample: PointerSample | null = null;
	let panVelocity = { x: 0, y: 0 };
	let pinch = $state<{ distance: number; worldX: number; worldY: number } | null>(null);
	const pointers = new Map<number, { x: number; y: number }>();
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
	const packedTrees = $derived.by(() => {
		const layouts = persistedRoots.map((root) => {
			const cached = treeCache.get(root.id)?.layout;
			return {
				rootId: root.id,
				createdAt: root.created_at,
				layout: cached ?? layoutLineageTree(rootLayoutNode(root))
			};
		});
		return packLineageForest(layouts);
	});
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
					shown.push({ rootId: packed.rootId, x, y, node });
					if (shown.length === MAX_MOUNTED_TILES) return shown;
				}
			}
		}
		return shown;
	});
	const forestBottom = $derived(
		Math.max(0, ...packedTrees.map((packed) => packed.y + packed.layout.height))
	);

	function rootLayoutNode(root: Generation): LineageLayoutNode<CanvasNodeData> {
		const asset = root.assets[0];
		return {
			id: asset.id,
			createdAt: root.created_at,
			data: {
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
		const next = new Map(treeCache);
		next.set(rootId, tree);
		treeCache = next;
	}

	async function loadRoots(): Promise<void> {
		if (rootsLoading || (!rootsHaveMore && roots.length > 0)) return;
		rootsLoading = true;
		rootsFailed = false;
		const cursor = roots.at(-1)?.id;
		const cursorQuery = cursor ? `&cursor=${cursor}` : '';
		try {
			const response = await fetch(
				`/api/v1/generations?roots_only=true&limit=${ROOT_LIMIT}${cursorQuery}`
			);
			if (!response.ok) throw new Error('history request failed');
			const page = (await response.json()) as Generation[];
			const existing = new Set(roots.map((root) => root.id));
			roots = [...roots, ...page.filter((root) => !existing.has(root.id))];
			rootsHaveMore = page.length === ROOT_LIMIT;
		} catch {
			rootsFailed = roots.length === 0;
		} finally {
			rootsLoading = false;
		}
	}

	async function loadTree(root: Generation, force = false): Promise<void> {
		const existing = treeCache.get(root.id);
		if (existing?.status === 'loading' || (existing?.status === 'loaded' && !force)) return;
		setCachedTree(root.id, { status: 'loading', layout: existing?.layout ?? null });
		const rootNode = rootLayoutNode(root);
		const nodesByAsset = new Map<string, LineageLayoutNode<CanvasNodeData>>([
			[rootNode.id, rootNode]
		]);
		const queue = [{ jobId: root.id, assetId: rootNode.id }];
		const visitedJobs = new Set<string>();
		try {
			while (queue.length > 0) {
				const target = queue.shift();
				if (!target || visitedJobs.has(target.jobId)) continue;
				visitedJobs.add(target.jobId);
				const [lineageResponse, generationResponse] = await Promise.all([
					fetch(`/api/v1/generations/${target.jobId}/lineage`),
					target.jobId === root.id
						? Promise.resolve(null)
						: fetch(`/api/v1/generations/${target.jobId}`)
				]);
				if (!lineageResponse.ok) {
					if (target.jobId === root.id) throw new Error('root lineage request failed');
					continue;
				}
				const parent = nodesByAsset.get(target.assetId);
				if (!parent) continue;
				if (generationResponse?.ok) {
					parent.data.generation = (await generationResponse.json()) as Generation;
				}
				const lineage = (await lineageResponse.json()) as GenerationLineage;
				for (const entry of lineage.children) {
					let child = nodesByAsset.get(entry.asset_id);
					if (!child) {
						child = {
							id: entry.asset_id,
							createdAt: entry.created_at,
							data: { entry, generation: null },
							children: []
						};
						nodesByAsset.set(entry.asset_id, child);
					}
					if (!parent.children.some((item) => item.id === child?.id)) parent.children.push(child);
					if (entry.job_id !== null) queue.push({ jobId: entry.job_id, assetId: entry.asset_id });
				}
			}
			const layout = layoutLineageTree(rootNode);
			const previousIds = new Set(existing?.layout?.nodes.map((node) => node.id) ?? []);
			const added = layout.nodes.map((node) => node.id).filter((id) => !previousIds.has(id));
			setCachedTree(root.id, { status: 'loaded', layout });
			if (!reducedMotion && previousIds.size > 0 && added.length > 0) {
				newNodeIds = new Set([...newNodeIds, ...added]);
				setTimeout(() => {
					newNodeIds = new Set([...newNodeIds].filter((id) => !added.includes(id)));
				}, 240);
			}
		} catch {
			setCachedTree(root.id, { status: 'error', layout: existing?.layout ?? null });
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
			if (treeCache.has(tree.rootId) || !treeIsVisible(tree)) continue;
			const root = persistedRoots.find((item) => item.id === tree.rootId);
			if (root) void loadTree(root);
		}
		if (rootsHaveMore && worldRect.bottom >= forestBottom - 320) void loadRoots();
	});

	$effect(() => {
		const finished = studio.history.filter((generation) => generation.assets.length > 0);
		for (const generation of finished) {
			if (knownFinishedIds.has(generation.id)) continue;
			knownFinishedIds.add(generation.id);
			if (generation.source_asset_id === null) {
				roots = [generation, ...roots.filter((root) => root.id !== generation.id)];
				if (!reducedMotion) newNodeIds = new Set([...newNodeIds, generation.assets[0].id]);
				continue;
			}
			for (const [rootId, cached] of treeCache) {
				if (!cached.layout?.nodes.some((node) => node.id === generation.source_asset_id)) continue;
				const root = roots.find((item) => item.id === rootId);
				if (root) void loadTree(root, true);
				break;
			}
		}
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
			translateX += panVelocity.x * elapsed;
			translateY += panVelocity.y * elapsed;
			inertiaFrame = requestAnimationFrame(step);
		};
		inertiaFrame = requestAnimationFrame(step);
	}

	function zoomAt(nextScale: number, cursorX: number, cursorY: number): void {
		const clamped = clampScale(nextScale);
		const worldX = (cursorX - translateX) / scale;
		const worldY = (cursorY - translateY) / scale;
		translateX = cursorX - worldX * clamped;
		translateY = cursorY - worldY * clamped;
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
		if ((event.target as Element).closest('button')) return;
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
			translateX = midpointX - pinch.worldX * nextScale;
			translateY = midpointY - pinch.worldY * nextScale;
			scale = nextScale;
			pinch = { distance, worldX: pinch.worldX, worldY: pinch.worldY };
			return;
		}
		if (panPointerId !== event.pointerId) return;
		translateX = panStart.translateX + event.clientX - panStart.x;
		translateY = panStart.translateY + event.clientY - panStart.y;
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
		pointers.delete(event.pointerId);
		if (viewportEl?.hasPointerCapture(event.pointerId))
			viewportEl.releasePointerCapture(event.pointerId);
		if (pointers.size < 2) pinch = null;
		if (panPointerId !== event.pointerId) return;
		panPointerId = null;
		lastPanSample = null;
		startInertia();
	}

	function recenterNewest(animate = true): void {
		const newest = [...persistedRoots].sort(
			(left, right) =>
				right.created_at.localeCompare(left.created_at) || left.id.localeCompare(right.id)
		)[0];
		if (!newest) return;
		const packed = packedTrees.find((tree) => tree.rootId === newest.id);
		const rootNode = packed?.layout.nodes.find((node) => node.id === packed.layout.rootId);
		if (!packed || !rootNode) return;
		stopInertia();
		if (animate && !reducedMotion) {
			recentering = true;
			if (recenterTimer) clearTimeout(recenterTimer);
			recenterTimer = setTimeout(() => (recentering = false), 260);
		}
		translateX = viewportWidth / 2 - (packed.x + rootNode.x) * scale;
		translateY = viewportHeight / 2 - (packed.y + rootNode.y) * scale;
	}

	function onKeyDown(event: KeyboardEvent): void {
		if (event.key === 'ArrowLeft') translateX += PAN_STEP;
		else if (event.key === 'ArrowRight') translateX -= PAN_STEP;
		else if (event.key === 'ArrowUp') translateY += PAN_STEP;
		else if (event.key === 'ArrowDown') translateY -= PAN_STEP;
		else if (event.key === '+' || event.key === '=') {
			zoomAt(scale * 1.2, viewportWidth / 2, viewportHeight / 2);
		} else if (event.key === '-' || event.key === '_') {
			zoomAt(scale / 1.2, viewportWidth / 2, viewportHeight / 2);
		} else if (event.key === 'Home') recenterNewest();
		else return;
		event.preventDefault();
	}

	function edgePath(
		tree: PackedLineageTree<CanvasNodeData>,
		source: PositionedLineageNode<CanvasNodeData>,
		target: PositionedLineageNode<CanvasNodeData>
	): string {
		const sourceX = tree.x + source.x;
		const sourceY = tree.y + source.y;
		const targetX = tree.x + target.x;
		const targetY = tree.y + target.y;
		const middleX = (sourceX + targetX) / 2;
		return `M ${sourceX} ${sourceY} C ${middleX} ${sourceY}, ${middleX} ${targetY}, ${targetX} ${targetY}`;
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
			const response = await fetch(`/api/v1/generations/${data.entry.job_id}`);
			if (!response.ok) throw new Error('generation refresh failed');
			const generation = (await response.json()) as Generation;
			failedImageIds = new Set([...failedImageIds].filter((id) => id !== assetId));
			replaceGeneration(assetId, generation);
		} catch {
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
		const clearPointer = () => (pointerWorld = null);
		node.addEventListener('pointerleave', clearPointer);
		return () => {
			node.removeEventListener('wheel', onWheel);
			node.removeEventListener('keydown', onKeyDown);
			node.removeEventListener('pointerdown', onPointerDown);
			node.removeEventListener('pointermove', onPointerMove);
			node.removeEventListener('pointerup', onPointerEnd);
			node.removeEventListener('pointercancel', onPointerEnd);
			node.removeEventListener('pointerleave', clearPointer);
		};
	}

	onMount(() => {
		reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		const resize = new ResizeObserver(([entry]) => {
			viewportWidth = entry.contentRect.width;
			viewportHeight = entry.contentRect.height;
		});
		if (viewportEl) resize.observe(viewportEl);
		void loadRoots().then(() => requestAnimationFrame(() => recenterNewest(false)));
		return () => resize.disconnect();
	});

	onDestroy(() => {
		stopInertia();
		if (recenterTimer) clearTimeout(recenterTimer);
	});
</script>

<div class="flex h-full min-h-0 flex-col gap-3">
	<header class="shrink-0">
		<h1 class="text-xl font-semibold">{t('app.images.title')}</h1>
		<p class="text-muted-foreground mt-1 text-sm">{t('app.images.sub')}</p>
	</header>
	<!-- A focusable canvas region owns the documented pan and zoom keyboard controls. -->
	<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
	<div
		bind:this={viewportEl}
		{@attach canvasInteractions}
		class="lineage-viewport border-border bg-card/20 relative min-h-0 flex-1 overflow-hidden rounded-lg border"
		class:is-panning={panPointerId !== null || pinch !== null}
		role="application"
		aria-label={t('app.images.canvas')}
		tabindex="0"
	>
		<div class="absolute end-3 top-3 z-30 flex gap-1">
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
			<div class="text-muted-foreground absolute inset-0 grid place-items-center text-sm">
				{t('app.images.load_failed')}
			</div>
		{:else if persistedRoots.length === 0}
			<div class="text-muted-foreground absolute inset-0 grid place-items-center text-sm">
				{t('app.gen.result_hint')}
			</div>
		{/if}

		<div
			class={`lineage-world lod-${lod}`}
			class:recentering
			style={`transform: translate3d(${translateX}px, ${translateY}px, 0) scale(${scale})`}
		>
			<svg class="lineage-edges" aria-hidden="true">
				{#each packedTrees as tree (tree.rootId)}
					{#each tree.layout.edges as edge (edge.id)}
						{@const edgeLeft = tree.x + Math.min(edge.source.x, edge.target.x)}
						{@const edgeTop = tree.y + Math.min(edge.source.y, edge.target.y)}
						{@const edgeRight = tree.x + Math.max(edge.source.x, edge.target.x)}
						{@const edgeBottom = tree.y + Math.max(edge.source.y, edge.target.y)}
						{#if rectsIntersect( worldRect, { left: edgeLeft, top: edgeTop, right: edgeRight, bottom: edgeBottom } )}
							<path d={edgePath(tree, edge.source, edge.target)} />
							<text
								x={tree.x + (edge.source.x + edge.target.x) / 2}
								y={tree.y + (edge.source.y + edge.target.y) / 2 - 7}
							>
								{actionLabel(edge.target.data.entry.action)}
							</text>
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
				<div
					class="tile-shell"
					class:is-new={newNodeIds.has(item.node.id)}
					style={`transform: translate3d(${item.x - LINEAGE_TILE_WIDTH / 2}px, ${item.y - LINEAGE_TILE_HEIGHT / 2}px, 0)`}
				>
					<button
						type="button"
						class="lineage-tile"
						class:is-selected={studio.selectedId === data.entry.job_id}
						class:is-missing={data.entry.missing || shownImage === null}
						style={`--tile-pull: ${proximityScale(item)}`}
						aria-label={`${actionLabel(data.entry.action)}: ${promptLabel(data)}`}
						aria-disabled={data.entry.job_id === null}
						title={promptLabel(data)}
						onfocus={() => (focusedNodeId = item.node.id)}
						onblur={() => (focusedNodeId = null)}
						onclick={() => data.entry.job_id !== null && void selectGeneration(data.entry.job_id)}
					>
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
				</div>
			{/each}
		</div>
	</div>
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

	.lineage-edges path {
		fill: none;
		stroke: var(--border);
		stroke-width: 2;
		vector-effect: non-scaling-stroke;
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

	.lineage-tile:focus-visible {
		outline: 3px solid var(--ring);
		outline-offset: 3px;
		transform: scale(1);
	}

	.lineage-tile.is-selected {
		border-color: var(--primary);
	}

	.lineage-tile[aria-disabled='true'] {
		cursor: default;
		opacity: 0.65;
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
		.lineage-tile {
			transition: none;
		}

		.tile-shell.is-new .lineage-tile {
			animation: none;
		}
	}
</style>
