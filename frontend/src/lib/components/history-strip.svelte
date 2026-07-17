<script lang="ts">
	import { onDestroy, tick } from 'svelte';
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
