import {
	layoutLineageTree,
	type LineageLayoutNode,
	type LineageTreeLayout
} from './lineage-layout.ts';
import type { Generation, GenerationSubtree, LineageEntry } from './studio.svelte.ts';

export type LineageNodeData = {
	output_asset_ids: string[];
	entry: LineageEntry;
	generation: Generation | null;
};

export type LineageTree = {
	status: 'loading' | 'loaded' | 'error';
	layout: LineageTreeLayout<LineageNodeData> | null;
	dirty: boolean;
	truncated: boolean;
	omittedHistoryJobIds: ReadonlySet<string>;
	remainingCountLowerBound: number;
	retrySpent: boolean;
};

type LoadRequest = {
	root: Generation;
	force: boolean;
	retry: boolean;
};

type LineageForestOptions = {
	fetchSubtree: (rootId: string, signal: AbortSignal) => Promise<GenerationSubtree>;
	initialHistory: Generation[];
	history: () => Generation[];
	onTreesChanged: (trees: ReadonlyMap<string, LineageTree>) => void;
	onRootLoaded: (root: Generation) => void;
	onArrived: (assetIds: string[]) => void;
	sessionCache?: Map<string, LineageTree>;
};

const sharedSessionCache = new Map<string, LineageTree>();
const MAX_CONCURRENT_LOADS = 4;

type CachedNode = {
	id: string;
	data: LineageNodeData;
};

function omittedHistoryJobIds(nodes: CachedNode[], history: Generation[]): Set<string> {
	const jobIds = new Set(
		nodes.map((node) => node.data.entry.job_id).filter((id): id is string => id !== null)
	);
	const assetIds = new Set(nodes.map((node) => node.id));
	const owners = new Map<string, string>();
	for (const generation of history) {
		for (const asset of generation.assets) owners.set(asset.id, generation.id);
	}
	return new Set(
		history
			.filter((generation) => {
				if (jobIds.has(generation.id) || generation.source_asset_id === null) return false;
				const parentId = owners.get(generation.source_asset_id);
				return (
					assetIds.has(generation.source_asset_id) || Boolean(parentId && jobIds.has(parentId))
				);
			})
			.map((generation) => generation.id)
	);
}

function needsHistoryRefresh(
	nodes: CachedNode[],
	history: Generation[],
	omittedIds: ReadonlySet<string>
): boolean {
	const nodeByJob = new Map(
		nodes
			.filter((node) => node.data.entry.job_id !== null)
			.map((node) => [node.data.entry.job_id as string, node])
	);
	const assetIds = new Set(
		nodes.flatMap((node) => node.data.output_asset_ids ?? [node.data.entry.asset_id])
	);
	const owners = new Map(
		history.flatMap((generation) =>
			generation.assets.map((asset) => [asset.id, generation.id] as const)
		)
	);
	for (const generation of history) {
		if (generation.assets.length === 0) continue;
		const cached = nodeByJob.get(generation.id);
		if (cached && !generation.assets.some((asset) => asset.id === cached.data.entry.asset_id)) {
			return true;
		}
		if (!cached && generation.source_asset_id && !omittedIds.has(generation.id)) {
			const parentId = owners.get(generation.source_asset_id);
			if (
				assetIds.has(generation.source_asset_id) ||
				Boolean(parentId && nodeByJob.has(parentId))
			) {
				return true;
			}
		}
	}
	return false;
}

export class LineageForest {
	readonly #fetchSubtree: LineageForestOptions['fetchSubtree'];
	readonly #history: LineageForestOptions['history'];
	readonly #onTreesChanged: LineageForestOptions['onTreesChanged'];
	readonly #onRootLoaded: LineageForestOptions['onRootLoaded'];
	readonly #onArrived: LineageForestOptions['onArrived'];
	readonly #sessionCache: Map<string, LineageTree>;
	readonly #knownFinishedIds: Set<string>;
	readonly #controllers = new Set<AbortController>();
	readonly #queue = new Map<string, LoadRequest>();
	#trees: Map<string, LineageTree>;
	#inFlight = 0;
	#active = true;

	constructor(options: LineageForestOptions) {
		this.#fetchSubtree = options.fetchSubtree;
		this.#history = options.history;
		this.#onTreesChanged = options.onTreesChanged;
		this.#onRootLoaded = options.onRootLoaded;
		this.#onArrived = options.onArrived;
		this.#sessionCache = options.sessionCache ?? sharedSessionCache;
		this.#trees = new Map(this.#sessionCache);
		this.#knownFinishedIds = new Set(
			options.initialHistory
				.filter((generation) => generation.assets.length > 0)
				.map((generation) => generation.id)
		);
	}

	get trees(): ReadonlyMap<string, LineageTree> {
		return this.#trees;
	}

	ensureVisible(root: Generation, fallback: LineageTreeLayout<LineageNodeData>): void {
		const cached = this.#trees.get(root.id);
		if (!root.has_derivatives) {
			if (cached === undefined) {
				this.#set(root.id, {
					status: 'loaded',
					layout: fallback,
					dirty: false,
					truncated: false,
					omittedHistoryJobIds: new Set(),
					remainingCountLowerBound: 0,
					retrySpent: false
				});
			}
			return;
		}
		if (cached === undefined) {
			this.#schedule({ root, force: false, retry: false });
		} else if (cached.status === 'error' && !cached.retrySpent) {
			this.#schedule({ root, force: false, retry: true });
		}
	}

	force(root: Generation): void {
		this.#schedule({ root, force: true, retry: false });
	}

	reconcileFinished(
		roots: Generation[],
		history: Generation[],
		starredOnly: boolean,
		isStarred: (id: string) => boolean
	): Generation[] {
		let nextRoots = roots;
		for (const generation of history) {
			if (generation.assets.length === 0 || this.#knownFinishedIds.has(generation.id)) continue;
			this.#knownFinishedIds.add(generation.id);
			if (generation.source_asset_id === null) {
				if (starredOnly && !isStarred(generation.id)) continue;
				nextRoots = [
					{ ...generation, has_derivatives: generation.has_derivatives ?? false },
					...nextRoots.filter((root) => root.id !== generation.id)
				];
				this.#onArrived([generation.assets[0].id]);
				continue;
			}
			for (const [rootId, cached] of this.#trees) {
				if (!cached.layout?.nodes.some((node) => node.id === generation.source_asset_id)) continue;
				nextRoots = nextRoots.map((root) =>
					root.id === rootId ? { ...root, has_derivatives: true } : root
				);
				const visibleRoot = nextRoots.find((root) => root.id === rootId);
				if (visibleRoot) {
					this.force(visibleRoot);
				} else if (cached.status === 'loading') {
					this.#sessionCache.delete(rootId);
					if (!cached.dirty) this.#set(rootId, { ...cached, dirty: true });
				} else {
					this.#invalidate(rootId);
				}
				break;
			}
		}
		return nextRoots;
	}

	reconcileHistory(roots: Generation[], history: Generation[]): void {
		for (const root of roots) {
			const cached = this.#trees.get(root.id);
			const cachedRoot = cached?.layout?.nodes.find((node) => node.data.entry.job_id === root.id);
			const derivativeFlagChanged =
				root.has_derivatives === true && cachedRoot?.data.generation?.has_derivatives !== true;
			if (
				cached?.status === 'loaded' &&
				cached.layout &&
				(derivativeFlagChanged ||
					needsHistoryRefresh(cached.layout.nodes, history, cached.omittedHistoryJobIds))
			) {
				this.force(root);
			}
		}
	}

	replaceGeneration(roots: Generation[], assetId: string, generation: Generation): Generation[] {
		const nextRoots = roots.map((root) => (root.id === generation.id ? generation : root));
		for (const [rootId, cached] of this.#trees) {
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
			this.#set(rootId, { ...cached, layout: { ...cached.layout, nodes } });
		}
		return nextRoots;
	}

	stop(): void {
		this.#active = false;
		this.#queue.clear();
		for (const controller of this.#controllers) controller.abort();
		this.#controllers.clear();
	}

	#schedule(request: LoadRequest): void {
		if (!this.#active) return;
		const cached = this.#trees.get(request.root.id);
		if (cached?.status === 'loading') {
			if (request.force && !cached.dirty) this.#set(request.root.id, { ...cached, dirty: true });
			return;
		}
		if (cached?.status === 'loaded' && !request.force) return;
		const queued = this.#queue.get(request.root.id);
		if (queued) {
			if (request.force && !queued.force) this.#queue.set(request.root.id, request);
			return;
		}
		this.#queue.set(request.root.id, request);
		this.#drain();
	}

	#drain(): void {
		while (this.#active && this.#inFlight < MAX_CONCURRENT_LOADS) {
			const next = this.#queue.entries().next().value as [string, LoadRequest] | undefined;
			if (!next) return;
			this.#queue.delete(next[0]);
			this.#inFlight += 1;
			void this.#load(next[1]).finally(() => {
				this.#inFlight -= 1;
				this.#drain();
			});
		}
	}

	async #load(request: LoadRequest): Promise<void> {
		const existing = this.#trees.get(request.root.id);
		const retrySpent = request.force ? false : request.retry || (existing?.retrySpent ?? false);
		this.#set(request.root.id, {
			status: 'loading',
			layout: existing?.layout ?? null,
			dirty: false,
			truncated: existing?.truncated ?? false,
			omittedHistoryJobIds: existing?.omittedHistoryJobIds ?? new Set(),
			remainingCountLowerBound: existing?.remainingCountLowerBound ?? 0,
			retrySpent
		});
		const controller = new AbortController();
		this.#controllers.add(controller);
		try {
			const subtree = await this.#fetchSubtree(request.root.id, controller.signal);
			if (!this.#active) return;
			const layout = this.#layout(request.root.id, subtree);
			const previousIds = new Set(existing?.layout?.nodes.map((node) => node.id) ?? []);
			const added = layout.layout.nodes.map((node) => node.id).filter((id) => !previousIds.has(id));
			const rerun = this.#trees.get(request.root.id)?.dirty === true;
			this.#set(request.root.id, {
				status: 'loaded',
				layout: layout.layout,
				dirty: false,
				truncated: subtree.truncated,
				omittedHistoryJobIds: subtree.truncated
					? omittedHistoryJobIds(layout.layout.nodes, this.#history())
					: new Set(),
				remainingCountLowerBound: subtree.remaining_count_lower_bound,
				retrySpent: false
			});
			this.#onRootLoaded(layout.root);
			if (previousIds.size > 0) this.#onArrived(added);
			if (rerun) {
				this.#sessionCache.delete(request.root.id);
				this.#schedule({ root: layout.root, force: true, retry: false });
			}
		} catch (error) {
			if (!this.#active || (error instanceof DOMException && error.name === 'AbortError')) return;
			const rerun = this.#trees.get(request.root.id)?.dirty === true;
			this.#set(request.root.id, {
				status: 'error',
				layout: existing?.layout ?? null,
				dirty: false,
				truncated: existing?.truncated ?? false,
				omittedHistoryJobIds: existing?.omittedHistoryJobIds ?? new Set(),
				remainingCountLowerBound: existing?.remainingCountLowerBound ?? 0,
				retrySpent
			});
			if (rerun) this.#schedule({ root: request.root, force: true, retry: false });
		} finally {
			this.#controllers.delete(controller);
		}
	}

	#layout(
		rootId: string,
		subtree: GenerationSubtree
	): {
		root: Generation;
		layout: LineageTreeLayout<LineageNodeData>;
	} {
		const nodesByAsset = new Map<string, LineageLayoutNode<LineageNodeData>>();
		const nodesByJob = new Map<string, LineageLayoutNode<LineageNodeData>>();
		for (const node of subtree.nodes) {
			const layoutNode: LineageLayoutNode<LineageNodeData> = {
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
		const responseRoot = subtree.nodes.find((node) => node.entry.job_id === rootId);
		if (!responseRoot) throw new Error('root missing from subtree');
		const rootNode = nodesByAsset.get(responseRoot.entry.asset_id);
		if (!rootNode) throw new Error('root missing from subtree');
		return {
			root: responseRoot.generation,
			layout: layoutLineageTree(rootNode)
		};
	}

	#set(rootId: string, tree: LineageTree): void {
		if (!this.#active) return;
		this.#trees = new Map(this.#trees).set(rootId, tree);
		if (tree.status === 'loaded') this.#sessionCache.set(rootId, tree);
		this.#publish();
	}

	#publish(): void {
		if (this.#active) this.#onTreesChanged(this.#trees);
	}

	#invalidate(rootId: string): void {
		this.#queue.delete(rootId);
		if (this.#trees.delete(rootId)) this.#publish();
		this.#sessionCache.delete(rootId);
	}
}
