<script lang="ts">
	import { onDestroy, untrack } from 'svelte';
	import { Popover } from 'bits-ui';
	import { Button } from '$lib/components/ui/button';
	import { account } from '$lib/account.svelte';
	import { openViewFor } from '$lib/account-display';
	import { t } from '$lib/i18n.svelte';
	import { openService, studio } from '$lib/studio.svelte';
	import {
		markTourDone,
		settleStep,
		shouldAutoStart,
		stepFrom,
		STUDIO_TOUR_STEPS
	} from '$lib/studio-tour';

	// Index into STUDIO_TOUR_STEPS, or -1 while no step is on screen.
	let step = $state(-1);
	// The control the current step points at. Focus returns here when the tour closes.
	let anchor = $state<HTMLElement | null>(null);
	let hasBack = $state(false);
	let hasNext = $state(false);
	let nextButton = $state<HTMLElement | null>(null);
	const current = $derived(step === -1 ? null : STUDIO_TOUR_STEPS[step]);
	// True while Generate opens for a tour that began elsewhere. Plain on purpose:
	// the checks that read it run again when the view and the models change.
	let awaitingGenerate = false;
	onDestroy(() => {
		studio.tourRequest = null;
	});

	// A control is a step only when it is rendered and laid out. A control the
	// role cannot use, or a sidebar closed on a phone, is passed over.
	function isShown(name: string): boolean {
		const element = document.querySelector(`[data-tour="${name}"]`);
		return element !== null && element.getClientRects().length > 0;
	}

	function anchorStillShown(): boolean {
		return anchor !== null && anchor.isConnected && anchor.getClientRects().length > 0;
	}

	function showStep(index: number): void {
		step = index;
		anchor = document.querySelector<HTMLElement>(
			`[data-tour="${STUDIO_TOUR_STEPS[index].anchor}"]`
		);
		// A control in a scrolled column can sit below the fold. Bring it into view
		// first, so the card is placed against where the control really is.
		anchor?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
		refreshNav(index);
	}

	// Back and Next depend on what is on screen right now.
	function refreshNav(index: number): void {
		hasBack = stepFrom(STUDIO_TOUR_STEPS, index, -1, isShown) !== -1;
		hasNext = stepFrom(STUDIO_TOUR_STEPS, index, 1, isShown) !== -1;
	}

	// Every end of a shown tour counts as finished: Skip, Escape, Done, and a
	// control that leaves the page. Otherwise a phone layout that always drops a
	// control would restart the tour on every load.
	function finish(): void {
		if (step === -1) return;
		markTourDone(() => localStorage);
		step = -1;
		awaitingGenerate = false;
		studio.tourRequest = null;
	}

	// Next and Back look up their target at click time: the layout may have changed
	// since the buttons were last drawn.
	function next(): void {
		const following = stepFrom(STUDIO_TOUR_STEPS, step, 1, isShown);
		if (following === -1) finish();
		else showStep(following);
	}

	function back(): void {
		const previous = stepFrom(STUDIO_TOUR_STEPS, step, -1, isShown);
		if (previous !== -1) showStep(previous);
	}

	// The page changed under the card: a resize, a view switch, a control rendered
	// or removed. A control still on screen keeps the card. Otherwise the card moves
	// to the next step on screen, or the tour ends.
	function settle(): void {
		if (step === -1) return;
		if (anchorStillShown()) {
			refreshNav(step);
			return;
		}
		const settled = settleStep(STUDIO_TOUR_STEPS, step, isShown);
		if (settled === -1) finish();
		else showStep(settled);
	}

	// Generate holds the steps. Show tour from another view opens Generate first,
	// when the role may use it.
	function canOpenGenerate(): boolean {
		return openViewFor('generate', 'usage', account.current?.role ?? null) === 'generate';
	}

	// Shows the first step on screen once the models have answered, so the controls
	// exist. Closes the request when no step is on screen at all.
	function start(): void {
		if (!studio.tourRequest || step !== -1) return;
		if (studio.tourRequest === 'automatic' && studio.shellView !== 'generate') {
			studio.tourRequest = null;
			firstVisitChecked = false;
			return;
		}
		if (!studio.modelsLoaded) return;
		if (awaitingGenerate) {
			if (studio.shellView !== 'generate') return;
			awaitingGenerate = false;
		} else if (studio.shellView !== 'generate' && canOpenGenerate()) {
			awaitingGenerate = true;
			openService('generate');
			return;
		}
		const first = stepFrom(STUDIO_TOUR_STEPS, -1, 1, isShown);
		if (first === -1) studio.tourRequest = null;
		else showStep(first);
	}

	// Focus goes back to the control the tour pointed at. A disabled control cannot
	// take focus (Generate is disabled until there is a prompt), so the prompt field
	// takes it, then the canvas.
	function returnFocus(): void {
		const fallbacks = [
			document.querySelector<HTMLElement>('[data-tour="prompt"]'),
			document.querySelector<HTMLElement>('[data-tour="canvas"]')
		];
		for (const candidate of [anchor, ...fallbacks]) {
			if (!candidate) continue;
			candidate.focus();
			if (document.activeElement === candidate) return;
		}
	}

	// Help requests a replay, and the first visit requests the tour
	// once the models have answered. Changing the view or the models may let it start.
	$effect(() => {
		void studio.tourRequest;
		void studio.shellView;
		void studio.modelsLoaded;
		untrack(start);
	});

	$effect(() => {
		if (step !== -1 && studio.shellView !== 'generate' && studio.shellView !== 'images') {
			untrack(finish);
		}
	});

	// While the tour is open, any change to the page re-checks it: a control rendered
	// or removed, a view switched. One check per frame keeps streaming updates cheap.
	$effect(() => {
		if (!studio.tourRequest) return;
		let frame = 0;
		const observer = new MutationObserver(() => {
			if (frame !== 0) return;
			frame = requestAnimationFrame(() => {
				frame = 0;
				if (step === -1) start();
				else settle();
			});
		});
		observer.observe(document.body, { childList: true, subtree: true });
		return () => {
			observer.disconnect();
			if (frame !== 0) cancelAnimationFrame(frame);
		};
	});

	// First visit to Generate: wait until the models have answered, so the prompt,
	// model and generate controls are rendered for the tour to point at.
	let firstVisitChecked = false;
	$effect(() => {
		if (firstVisitChecked || !studio.modelsLoaded || studio.shellView !== 'generate') return;
		firstVisitChecked = true;
		if (shouldAutoStart(() => localStorage)) studio.tourRequest = 'automatic';
	});
</script>

<svelte:window onresize={settle} />

<!-- Non-modal: a click outside the card does not mark the tour as done. -->
<Popover.Root
	open={studio.tourRequest !== null && current !== null}
	onOpenChange={(open) => {
		if (!open) finish();
	}}
>
	<Popover.Content
		role="dialog"
		trapFocus={false}
		customAnchor={anchor}
		side="bottom"
		align="center"
		sideOffset={12}
		collisionPadding={16}
		onOpenAutoFocus={(event) => {
			event.preventDefault();
			nextButton?.focus();
		}}
		onCloseAutoFocus={(event) => {
			event.preventDefault();
			const focused = document.activeElement;
			if (
				focused === document.body ||
				document.querySelector('[data-studio-tour]')?.contains(focused)
			) {
				returnFocus();
			}
		}}
		onInteractOutside={(event) => event.preventDefault()}
		aria-labelledby="studio-tour-title"
		aria-describedby="studio-tour-body"
		data-studio-tour
		class="bg-popover text-popover-foreground ring-foreground/10 z-50 w-80 max-w-[calc(100vw-2rem)] rounded-2xl p-4 shadow-2xl ring-1 outline-none"
	>
		{#if current}
			<!-- The live region holds the step text, so each step change is announced. -->
			<div aria-live="polite">
				<h2 id="studio-tour-title" class="text-foreground text-sm font-semibold">
					{t(current.titleKey)}
				</h2>
				<p id="studio-tour-body" class="text-muted-foreground mt-1.5 text-[13px] leading-relaxed">
					{t(current.bodyKey)}
				</p>
			</div>
			<div class="mt-4 flex items-center justify-between gap-2">
				<Button variant="ghost" size="sm" onclick={finish}>{t('app.tour.skip')}</Button>
				<div class="flex items-center gap-2">
					<Button variant="outline" size="sm" disabled={!hasBack} onclick={back}>
						{t('app.tour.back')}
					</Button>
					<Button variant="brand" size="sm" bind:ref={nextButton} onclick={next}>
						{hasNext ? t('app.tour.next') : t('app.tour.done')}
					</Button>
				</div>
			</div>
		{/if}
	</Popover.Content>
</Popover.Root>
