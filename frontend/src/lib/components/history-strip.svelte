<script lang="ts">
	import { tick } from 'svelte';
	import { t } from '$lib/i18n.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import ChevronLeftIcon from '@lucide/svelte/icons/chevron-left';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import StarIcon from '@lucide/svelte/icons/star';
	import {
		isStarred,
		loadOlderHistory,
		resetHistoryToRecent,
		starredGenerations,
		studio,
		type Generation
	} from '$lib/studio.svelte';

	let stripEl = $state<HTMLDivElement | null>(null);
	let loadingOlder = $state(false);
	let loadError = $state('');

	// Click-drag horizontal scroll (scrollbar is hidden via no-scrollbar).
	// Capture only after the move threshold so plain clicks still select.
	let dragPointerId: number | null = null;
	let dragStartX = 0;
	let dragStartScroll = 0;
	let dragMoved = false;
	let suppressClick = false;
	const DRAG_THRESHOLD_PX = 12;

	const shownId = $derived(
		studio.selectedId ?? studio.history.find((g) => g.assets.length > 0)?.id ?? null
	);

	// Starred jobs outside the loaded history pages still appear at the front.
	const stripGenerations = $derived.by(() => {
		const seen = new Set<string>();
		const items: Generation[] = [];
		for (const generation of starredGenerations()) {
			if (seen.has(generation.id)) continue;
			seen.add(generation.id);
			items.push(generation);
		}
		for (const generation of studio.history) {
			if (generation.state === 'failed' || seen.has(generation.id)) continue;
			seen.add(generation.id);
			items.push(generation);
		}
		return items;
	});

	async function loadOlder(): Promise<void> {
		if (!stripEl || loadingOlder) return;
		loadingOlder = true;
		loadError = '';
		const loaded = await loadOlderHistory();
		await tick();
		if (loaded && stripEl) {
			stripEl.scrollLeft = stripEl.scrollWidth - stripEl.clientWidth;
		} else if (!loaded) {
			loadError = t('app.gen.load_older_empty');
		}
		loadingOlder = false;
	}

	async function backToRecent(): Promise<void> {
		await resetHistoryToRecent();
		await tick();
		stripEl?.scrollTo({ left: 0, behavior: 'smooth' });
	}

	function select(generation: Generation): void {
		studio.selectedId = generation.id;
	}

	function onStripPointerDown(event: PointerEvent): void {
		if (event.button !== 0 || !stripEl) return;
		dragPointerId = event.pointerId;
		dragStartX = event.clientX;
		dragStartScroll = stripEl.scrollLeft;
		dragMoved = false;
		suppressClick = false;
	}

	function onStripPointerMove(event: PointerEvent): void {
		if (dragPointerId === null || event.pointerId !== dragPointerId || !stripEl) return;
		const delta = event.clientX - dragStartX;
		if (!dragMoved) {
			if (Math.abs(delta) < DRAG_THRESHOLD_PX) return;
			dragMoved = true;
			suppressClick = true;
			stripEl.setPointerCapture(event.pointerId);
		}
		stripEl.scrollLeft = dragStartScroll - delta;
	}

	function endStripDrag(event: PointerEvent): void {
		if (dragPointerId === null || event.pointerId !== dragPointerId || !stripEl) return;
		if (stripEl.hasPointerCapture(event.pointerId)) {
			stripEl.releasePointerCapture(event.pointerId);
		}
		dragPointerId = null;
		dragMoved = false;
	}

	function onThumbClick(event: MouseEvent, generation: Generation): void {
		// A drag that moved past the threshold is scrolling, not a selection.
		if (suppressClick) {
			event.preventDefault();
			event.stopPropagation();
			suppressClick = false;
			return;
		}
		select(generation);
	}
</script>

{#if stripGenerations.length > 0}
	<div class="flex min-w-0 w-full items-stretch gap-2">
		{#if studio.historyExtended}
			<button
				type="button"
				class="border-border bg-muted/40 text-muted-foreground hover:bg-muted hover:text-foreground flex h-24 w-14 shrink-0 flex-col items-center justify-center gap-1 rounded-lg border text-[0.65rem] leading-tight transition-colors"
				title={t('app.gen.back_to_recent')}
				onclick={backToRecent}
			>
				<ChevronLeftIcon class="size-4" />
				<span class="max-w-[2.5rem] text-center">{t('app.gen.back_to_recent')}</span>
			</button>
		{/if}

		<div
			class="no-scrollbar flex min-w-0 flex-1 cursor-grab gap-2 overflow-x-auto pb-1 active:cursor-grabbing"
			bind:this={stripEl}
			onpointerdown={onStripPointerDown}
			onpointermove={onStripPointerMove}
			onpointerup={endStripDrag}
			onpointercancel={endStripDrag}
			role="list"
		>
			{#each stripGenerations as generation (generation.id)}
				{#if generation.assets.length > 0}
					<button
						type="button"
						class="relative shrink-0"
						title={generation.params.prompt}
						onclick={(event) => onThumbClick(event, generation)}
					>
						<img
							src={generation.assets[0].url}
							alt={generation.params.prompt ?? generation.id}
							class={'pointer-events-none h-24 w-24 rounded-lg border object-cover ' +
								(shownId === generation.id ? 'border-primary' : 'border-border')}
							draggable="false"
						/>
						{#if isStarred(generation.id)}
							<span
								class="bg-background/80 pointer-events-none absolute end-1 top-1 rounded-full p-0.5"
								aria-hidden="true"
							>
								<StarIcon class="fill-current size-3" />
							</span>
						{/if}
					</button>
				{:else if generation.state === 'queued' || generation.state === 'running'}
					<div
						class="border-border/60 text-muted-foreground relative grid h-24 w-24 shrink-0 place-items-center rounded-lg border border-dashed"
					>
						<Badge variant="outline">
							{t('app.gen.badge_working')}
						</Badge>
						{#if generation.state === 'running' && generation.progress !== null}
							<div class="bg-border absolute inset-x-3 bottom-2 h-1 rounded-full">
								<div
									class="bg-primary h-1 rounded-full transition-[width]"
									style={`width: ${Math.round(generation.progress * 100)}%`}
								></div>
							</div>
						{/if}
					</div>
				{/if}
			{/each}
		</div>

		{#if studio.historyHasMore}
			<button
				type="button"
				class="border-border bg-muted/40 text-muted-foreground hover:bg-muted hover:text-foreground flex h-24 w-14 shrink-0 flex-col items-center justify-center gap-1 rounded-lg border text-[0.65rem] leading-tight transition-colors disabled:opacity-50"
				title={t('app.gen.load_older')}
				disabled={loadingOlder}
				onclick={loadOlder}
			>
				<ChevronRightIcon class="size-4" />
				<span class="max-w-[2.5rem] text-center">{t('app.gen.load_older')}</span>
			</button>
		{/if}
	</div>
	{#if loadError !== ''}
		<p class="text-muted-foreground text-xs">{loadError}</p>
	{/if}
{/if}
