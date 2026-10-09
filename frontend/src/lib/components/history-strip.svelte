<script lang="ts">
	import { onDestroy, tick, untrack } from 'svelte';
	import { t } from '$lib/i18n.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import ChevronLeftIcon from '@lucide/svelte/icons/chevron-left';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import CircleXIcon from '@lucide/svelte/icons/circle-x';
	import StarIcon from '@lucide/svelte/icons/star';
	import { isCancellable } from '$lib/generation-state';
	import {
		cancelGeneration,
		isStarred,
		loadOlderHistory,
		resetHistoryToRecent,
		starredGenerations,
		studio,
		type Generation
	} from '$lib/studio.svelte';
	import { isStripNavKey, nextFocusIndex, thumbnailLabel } from '$lib/history-strip-nav';

	let stripEl = $state<HTMLDivElement | null>(null);
	let loadingOlder = $state(false);
	let loadError = $state('');
	let cancellingIds = $state<Set<string>>(new Set());
	let cancelError = $state('');
	// The roving tab stop, kept as the thumbnail's generation id rather than
	// its index: starred items and new results arrive at the front and would
	// otherwise move the stop onto another thumbnail. The others carry
	// tabindex -1, so tabbing lands in the strip once.
	let rovingId = $state<string | null>(null);

	async function cancelJob(id: string): Promise<void> {
		if (cancellingIds.has(id)) return;
		cancellingIds = new Set(cancellingIds).add(id);
		cancelError = '';
		const ok = await cancelGeneration(id);
		if (!ok) cancelError = t('app.gen.cancel_failed');
		const next = new Set(cancellingIds);
		next.delete(id);
		cancellingIds = next;
	}

	// Click-drag horizontal scroll (scrollbar is hidden via no-scrollbar).
	// Capture only after the move threshold so plain clicks still select.
	// On release, leftover velocity coasts with friction and may reach an end.
	let dragPointerId: number | null = null;
	let dragStartX = 0;
	let dragStartScroll = 0;
	let dragMoved = false;
	let suppressClick = false;
	let lastSampleX = 0;
	let lastSampleT = 0;
	let velocityPxPerMs = 0; // positive = toward higher scrollLeft
	let inertiaRaf = 0;
	const DRAG_THRESHOLD_PX = 12;
	const INERTIA_MIN_PX_PER_MS = 0.04;
	// Lower = longer coast. Tuned so a hard flick can cross many thumbs
	// without always slamming into the end.
	const INERTIA_FRICTION_PER_MS = 0.0028;
	const INERTIA_VELOCITY_BOOST = 1.15;
	const VELOCITY_EMA_ALPHA = 0.35;

	const shownId = $derived(
		studio.selectedId ?? studio.history.find((g) => g.assets.length > 0)?.id ?? null
	);

	function maxScrollLeft(el: HTMLDivElement): number {
		return Math.max(0, el.scrollWidth - el.clientWidth);
	}

	function stopInertia(): void {
		if (inertiaRaf !== 0) {
			cancelAnimationFrame(inertiaRaf);
			inertiaRaf = 0;
		}
	}

	function startFling(velocity: number): void {
		if (!stripEl) return;
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
		const max = maxScrollLeft(stripEl);
		if (max <= 0) return;

		let vel = velocity * INERTIA_VELOCITY_BOOST;
		if (Math.abs(vel) < INERTIA_MIN_PX_PER_MS) return;

		let prev = performance.now();
		const step = (now: number) => {
			if (!stripEl) {
				inertiaRaf = 0;
				return;
			}
			const dt = Math.min(32, now - prev);
			prev = now;
			// Exponential decay: force falls off over time/distance.
			vel *= Math.exp(-INERTIA_FRICTION_PER_MS * dt);
			if (Math.abs(vel) < INERTIA_MIN_PX_PER_MS) {
				inertiaRaf = 0;
				return;
			}
			const next = stripEl.scrollLeft + vel * dt;
			const end = maxScrollLeft(stripEl);
			stripEl.scrollLeft = Math.max(0, Math.min(end, next));
			if (stripEl.scrollLeft <= 0 || stripEl.scrollLeft >= end) {
				inertiaRaf = 0;
				return;
			}
			inertiaRaf = requestAnimationFrame(step);
		};
		inertiaRaf = requestAnimationFrame(step);
	}

	onDestroy(stopInertia);

	// Starred jobs outside the loaded history pages still appear at the front.
	// Each entry carries its thumbnail index: only entries with assets render
	// a thumbnail, so the roving focus counts those, not the working cards.
	type StripItem = { generation: Generation; thumbIndex: number };
	const stripItems = $derived.by(() => {
		const seen = new Set<string>();
		const items: StripItem[] = [];
		let thumbIndex = 0;
		const push = (generation: Generation) => {
			if (seen.has(generation.id)) return;
			seen.add(generation.id);
			items.push({
				generation,
				thumbIndex: generation.assets.length > 0 ? thumbIndex++ : -1
			});
		};
		for (const generation of starredGenerations()) {
			push(generation);
		}
		for (const generation of studio.history) {
			if (generation.state === 'failed') continue;
			push(generation);
		}
		return items;
	});

	const thumbIds = $derived(
		stripItems.filter((item) => item.thumbIndex >= 0).map((item) => item.generation.id)
	);
	// A stop whose thumbnail left the strip (a reset to recent, a removed job)
	// falls back to the first one, so a thumbnail is always reachable.
	const tabStop = $derived(Math.max(0, rovingId === null ? 0 : thumbIds.indexOf(rovingId)));

	// A new selection moves the stop to the selected thumbnail. Only a change of
	// selection does: reacting to the list would undo every arrow key, and a
	// running job rewrites the list on each progress event.
	$effect(() => {
		const id = shownId;
		if (id !== null && untrack(() => thumbIds).includes(id)) rovingId = id;
	});

	function onStripKeydown(event: KeyboardEvent): void {
		if (!isStripNavKey(event.key)) return;
		if (thumbIds.length === 0) return;
		event.preventDefault();
		const next = nextFocusIndex(tabStop, thumbIds.length, event.key);
		if (next === tabStop) return;
		rovingId = thumbIds[next];
		const thumb = stripEl?.querySelector<HTMLButtonElement>(`[data-strip-thumb="${next}"]`);
		thumb?.focus();
		thumb?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
	}

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
		const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		stripEl?.scrollTo({ left: 0, behavior: reduced ? 'auto' : 'smooth' });
	}

	function select(generation: Generation): void {
		studio.selectedId = generation.id;
	}

	function onStripPointerDown(event: PointerEvent): void {
		if (event.button !== 0 || !stripEl) return;
		stopInertia();
		dragPointerId = event.pointerId;
		dragStartX = event.clientX;
		dragStartScroll = stripEl.scrollLeft;
		dragMoved = false;
		suppressClick = false;
		lastSampleX = event.clientX;
		lastSampleT = event.timeStamp;
		velocityPxPerMs = 0;
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
		const sampleDt = event.timeStamp - lastSampleT;
		if (sampleDt > 0) {
			// Finger left => scrollLeft increases. EMA smooths noisy samples.
			const sample = (lastSampleX - event.clientX) / sampleDt;
			velocityPxPerMs =
				velocityPxPerMs === 0
					? sample
					: VELOCITY_EMA_ALPHA * sample + (1 - VELOCITY_EMA_ALPHA) * velocityPxPerMs;
		}
		lastSampleX = event.clientX;
		lastSampleT = event.timeStamp;
		stripEl.scrollLeft = dragStartScroll - delta;
	}

	function endStripDrag(event: PointerEvent): void {
		if (dragPointerId === null || event.pointerId !== dragPointerId || !stripEl) return;
		if (stripEl.hasPointerCapture(event.pointerId)) {
			stripEl.releasePointerCapture(event.pointerId);
		}
		const fling = dragMoved;
		// Ignore velocity if the pointer sat still before release.
		const releaseVelocity = event.timeStamp - lastSampleT > 80 ? 0 : velocityPxPerMs;
		dragPointerId = null;
		dragMoved = false;
		velocityPxPerMs = 0;
		if (fling) startFling(releaseVelocity);
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

{#if stripItems.length > 0}
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
			role="group"
			aria-label={t('app.gen.history_strip')}
		>
			{#each stripItems as item (item.generation.id)}
				{#if item.generation.assets.length > 0}
					<button
						type="button"
						class="relative shrink-0 hover:opacity-90 focus-visible:ring-ring/50 focus-visible:ring-[3px] outline-none rounded-md"
						title={thumbnailLabel(item.generation.params.prompt, t('app.gen.untitled'))}
						tabindex={item.thumbIndex === tabStop ? 0 : -1}
						aria-current={shownId === item.generation.id ? 'true' : undefined}
						data-strip-thumb={item.thumbIndex}
						onkeydown={onStripKeydown}
						onclick={(event) => onThumbClick(event, item.generation)}
					>
						<img
							src={item.generation.assets[0].thumbnail_url ?? item.generation.assets[0].url}
							alt={thumbnailLabel(item.generation.params.prompt, t('app.gen.untitled'))}
							class={'pointer-events-none h-24 w-24 rounded-lg border object-cover ' +
								(shownId === item.generation.id ? 'border-primary' : 'border-border')}
							width="96"
							height="96"
							loading="lazy"
							draggable="false"
						/>
						{#if isStarred(item.generation.id)}
							<span
								class="bg-background/80 pointer-events-none absolute end-1 top-1 rounded-full p-0.5"
								aria-hidden="true"
							>
								<StarIcon class="fill-current size-3" />
							</span>
						{/if}
					</button>
				{:else if isCancellable(item.generation.state)}
					<div
						class="border-border/60 text-muted-foreground relative grid h-24 w-24 shrink-0 place-items-center rounded-lg border border-dashed"
					>
						<Badge variant="outline">
							{t('app.gen.badge_working')}
						</Badge>
						<Button
							type="button"
							variant="outline"
							size="icon-xs"
							class="bg-background/80 absolute end-1 top-1"
							title={t('app.gen.cancel')}
							aria-label={t('app.gen.cancel')}
							disabled={cancellingIds.has(item.generation.id)}
							onclick={() => cancelJob(item.generation.id)}
						>
							<CircleXIcon />
						</Button>
						{#if item.generation.state === 'running' && item.generation.progress !== null}
							<div class="bg-border absolute inset-x-3 bottom-2 h-1 rounded-full">
								<div
									role="progressbar"
									aria-valuemin="0"
									aria-valuemax="100"
									aria-valuenow={Math.round(item.generation.progress * 100)}
									aria-label={t('app.gen.badge_working')}
									class="bg-primary h-1 w-full origin-left rounded-full transition-transform motion-reduce:transition-none"
									style={`transform: scaleX(${item.generation.progress})`}
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
		<p role="alert" class="text-muted-foreground text-xs">{loadError}</p>
	{/if}
	{#if cancelError !== ''}
		<p role="status" class="text-destructive text-xs">{cancelError}</p>
	{/if}
{/if}
