<script lang="ts">
	import { onDestroy, tick, untrack } from 'svelte';
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
	let card = $state<HTMLElement | null>(null);
	let body = $state<HTMLElement | null>(null);
	let bodyOverflows = $state(false);
	let rect = $state({ left: 0, top: 0, width: 0, height: 0 });
	let position = $state({ left: 16, top: 16, side: 'inside', arrow: 20 });
	let visibleSteps = $state<number[]>([]);

	function target(name: string): HTMLElement | null {
		if (name === 'install-model' && studio.models.length !== 0) return null;
		const element = document.querySelector<HTMLElement>(`[data-tour="${name}"]`);
		if (element && element.getClientRects().length > 0) return element;
		if (name === 'images' || name === 'install-model') {
			const trigger = [...document.querySelectorAll<HTMLElement>('[data-sidebar="trigger"]')].find(
				(element) => element.getClientRects().length > 0
			);
			if (trigger) return trigger;
		}
		return null;
	}

	async function place(): Promise<void> {
		await tick();
		if (!anchor || !card || step === -1) return;
		bodyOverflows = !!body && body.scrollHeight > body.clientHeight;
		const r = anchor.getBoundingClientRect();
		const left = Math.max(0, r.left),
			top = Math.max(0, r.top);
		const right = Math.min(innerWidth, r.right),
			bottom = Math.min(innerHeight, r.bottom);
		rect = { left, top, width: Math.max(0, right - left), height: Math.max(0, bottom - top) };
		const w = card.offsetWidth,
			h = card.offsetHeight,
			gap = 16,
			pad = 16;
		let x = left + (right - left - w) / 2,
			y = top + (bottom - top - h) / 2;
		let side = 'inside';
		if (STUDIO_TOUR_STEPS[step].anchor === 'canvas') {
			// The canvas is the destination, so keep its card within it.
		} else if (right + gap + w <= innerWidth - pad) {
			x = right + gap;
			side = 'right';
		} else if (left - gap - w >= pad) {
			x = left - gap - w;
			side = 'left';
		} else if (bottom + gap + h <= innerHeight - pad) {
			y = bottom + gap;
			side = 'bottom';
		} else if (top - gap - h >= pad) {
			y = top - gap - h;
			side = 'top';
		}
		x = Math.max(pad, Math.min(x, innerWidth - w - pad));
		y = Math.max(pad, Math.min(y, innerHeight - h - pad));
		const horizontal = side === 'top' || side === 'bottom';
		const offset = horizontal ? (left + right) / 2 - x : (top + bottom) / 2 - y;
		const size = horizontal ? w : h;
		position = { left: x, top: y, side, arrow: Math.max(16, Math.min(offset, size - 16)) - 6 };
	}

	function keydown(event: KeyboardEvent): void {
		if (step === -1) return;
		if (event.key === 'Escape') {
			event.preventDefault();
			finish();
		}
		if (event.key === 'Tab' && card) {
			const buttons = [
				...card.querySelectorAll<HTMLElement>('button:not(:disabled), [tabindex="0"]')
			];
			const index = buttons.indexOf(
				document.activeElement instanceof HTMLElement ? document.activeElement : buttons[0]
			);
			if (event.shiftKey && index <= 0) {
				event.preventDefault();
				buttons.at(-1)?.focus();
			} else if (!event.shiftKey && index === buttons.length - 1) {
				event.preventDefault();
				buttons[0]?.focus();
			}
		}
	}
	const current = $derived(step === -1 ? null : STUDIO_TOUR_STEPS[step]);
	// True while Generate opens for a tour that began elsewhere. Plain on purpose:
	// the checks that read it run again when the view and the models change.
	let awaitingGenerate = false;
	onDestroy(() => {
		studio.tourRequest = null;
		studio.tourOpen = false;
	});

	// A control is a step only when it is rendered and laid out. A control the
	// role cannot use, or a sidebar closed on a phone, is passed over.
	function isShown(name: string): boolean {
		return target(name) !== null;
	}

	function anchorStillShown(): boolean {
		return anchor !== null && anchor.isConnected && anchor.getClientRects().length > 0;
	}

	function showStep(index: number): void {
		step = index;
		if (body) body.scrollTop = 0;
		studio.tourOpen = true;
		anchor = target(STUDIO_TOUR_STEPS[index].anchor);
		// A control in a scrolled column can sit below the fold. Bring it into view
		// first, so the card is placed against where the control really is.
		void tick().then(() => {
			anchor?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
			void place();
		});
		refreshNav(index);
		void place();
		void tick().then(() => nextButton?.focus());
	}

	// Back and Next depend on what is on screen right now.
	function refreshNav(index: number): void {
		visibleSteps = STUDIO_TOUR_STEPS.map((_, i) => i).filter((i) =>
			isShown(STUDIO_TOUR_STEPS[i].anchor)
		);
		hasBack = stepFrom(STUDIO_TOUR_STEPS, index, -1, isShown) !== -1;
		hasNext = stepFrom(STUDIO_TOUR_STEPS, index, 1, isShown) !== -1;
	}

	// Every end of a shown tour counts as finished: Skip, Escape, Done, and a
	// control that leaves the page. Otherwise a phone layout that always drops a
	// control would restart the tour on every load.
	function finish(): void {
		if (step === -1) return;
		markTourDone(() => localStorage);
		const focused = document.activeElement;
		const shouldRestore = focused === document.body || card?.contains(focused);
		step = -1;
		studio.tourOpen = false;
		if (shouldRestore) void tick().then(returnFocus);
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
		const updated = target(STUDIO_TOUR_STEPS[step].anchor);
		anchor = updated;
		if (anchorStillShown()) {
			refreshNav(step);
			void place();
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

	function focusIsOccupied(): boolean {
		const active = document.activeElement;
		if (
			active instanceof HTMLElement &&
			(active.isContentEditable ||
				active.matches(
					'textarea, input:not([type]), input[type="text"], input[type="search"], input[type="email"], input[type="url"], input[type="tel"], input[type="password"], input[type="number"]'
				))
		)
			return true;
		return !!document.querySelector(
			'[data-state="open"][role="dialog"], [data-state="open"][role="menu"], [data-state="open"][role="listbox"], [data-bits-floating-content-wrapper] [data-state="open"]'
		);
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
		if (studio.tourRequest === 'automatic' && focusIsOccupied()) return;
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
		const schedule = () => {
			if (frame !== 0) return;
			frame = requestAnimationFrame(() => {
				frame = 0;
				if (step === -1) start();
				else settle();
			});
		};
		const observer = new MutationObserver(schedule);
		document.addEventListener('focusout', schedule);
		observer.observe(document.body, {
			childList: true,
			subtree: true,
			attributes: true,
			attributeFilter: ['data-state', 'role']
		});
		return () => {
			observer.disconnect();
			document.removeEventListener('focusout', schedule);
			if (frame !== 0) cancelAnimationFrame(frame);
		};
	});

	$effect(() => {
		void studio.models.length;
		if (step !== -1) untrack(settle);
	});

	$effect(() => {
		if (step === -1 || !anchor) return;
		const observer = new ResizeObserver(() => void place());
		observer.observe(anchor);
		if (card) observer.observe(card);
		if (body) observer.observe(body);
		const reposition = () => void place();
		document.addEventListener('scroll', reposition, true);
		document.addEventListener('transitionend', reposition, true);
		return () => {
			observer.disconnect();
			document.removeEventListener('scroll', reposition, true);
			document.removeEventListener('transitionend', reposition, true);
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

<svelte:window onresize={settle} onscroll={place} onkeydown={keydown} onpagehide={finish} />

{#if current}
	<div class="tour-blocker" aria-hidden="true"></div>
	<div
		data-tour-ring
		class="tour-ring"
		aria-hidden="true"
		style:left="{rect.left}px"
		style:top="{rect.top}px"
		style:width="{rect.width}px"
		style:height="{rect.height}px"
	></div>
	<div
		bind:this={card}
		data-studio-tour
		role="dialog"
		aria-modal="true"
		aria-labelledby="studio-tour-title"
		aria-describedby="studio-tour-body"
		class="tour-card bg-popover text-popover-foreground"
		style:left="{position.left}px"
		style:top="{position.top}px"
		data-side={position.side}
	>
		{#if position.side !== 'inside'}<span
				class="tour-arrow"
				aria-hidden="true"
				style:--arrow="{position.arrow}px"
			></span>{/if}
		<div
			bind:this={body}
			aria-live="polite"
			class="tour-body"
			role={bodyOverflows ? 'region' : undefined}
			aria-labelledby={bodyOverflows ? 'studio-tour-title' : undefined}
			tabindex={bodyOverflows ? 0 : undefined}
		>
			<p class="tour-counter">
				{t('app.tour.counter')
					.replace('{step}', String(visibleSteps.indexOf(step) + 1))
					.replace('{total}', String(visibleSteps.length))}
			</p>
			<h2 id="studio-tour-title" class="text-foreground mt-2 text-base font-semibold">
				{t(current.titleKey)}
			</h2>
			<p id="studio-tour-body" class="text-muted-foreground mt-2 text-sm leading-relaxed">
				{current.anchor === 'images' && anchor?.dataset.sidebar === 'trigger'
					? t('app.tour.images_mobile')
					: current.anchor === 'install-model' && anchor?.dataset.sidebar === 'trigger'
						? t('app.tour.install_mobile')
						: t(current.bodyKey)}
			</p>
		</div>
		<div class="mt-5 flex flex-wrap items-center justify-between gap-2">
			<Button variant="ghost" size="sm" onclick={finish}>{t('app.tour.skip')}</Button>
			<div class="flex flex-wrap items-center gap-2">
				<Button variant="outline" size="sm" disabled={!hasBack} onclick={back}
					>{t('app.tour.back')}</Button
				>
				<Button variant="brand" size="sm" bind:ref={nextButton} onclick={next}
					>{hasNext ? t('app.tour.next') : t('app.tour.done')}</Button
				>
			</div>
		</div>
	</div>
{/if}

<style>
	.tour-blocker {
		position: fixed;
		inset: 0;
		z-index: 90;
	}
	.tour-ring {
		position: fixed;
		z-index: 91;
		pointer-events: none;
		border: 3px solid #6685ff;
		border-radius: 10px;
		box-shadow:
			0 0 0 9999px rgb(0 0 0 / 0.7),
			0 0 20px rgb(93 126 255 / 0.5);
	}
	.tour-card {
		position: fixed;
		z-index: 92;
		width: min(360px, calc(100vw - 32px));
		max-height: calc(100dvh - 32px);
		display: flex;
		flex-direction: column;
		border: 1px solid #6685ff;
		border-radius: 16px;
		padding: 20px;
		box-shadow: 0 20px 60px rgb(0 0 0 / 0.55);
		animation: tour-enter 160ms ease-out;
	}
	.tour-body {
		min-height: 0;
		overflow-y: auto;
	}
	.tour-counter {
		color: #819aff;
		font-size: 12px;
		font-weight: 700;
		letter-spacing: 0.04em;
	}
	.tour-arrow {
		position: absolute;
		width: 12px;
		height: 12px;
		background: inherit;
		border: solid #6685ff;
		transform: rotate(45deg);
	}
	[data-side='right'] .tour-arrow {
		left: -7px;
		top: var(--arrow);
		border-width: 0 0 1px 1px;
	}
	[data-side='left'] .tour-arrow {
		right: -7px;
		top: var(--arrow);
		border-width: 1px 1px 0 0;
	}
	[data-side='bottom'] .tour-arrow {
		top: -7px;
		left: var(--arrow);
		border-width: 1px 0 0 1px;
	}
	[data-side='top'] .tour-arrow {
		bottom: -7px;
		left: var(--arrow);
		border-width: 0 1px 1px 0;
	}
	@keyframes tour-enter {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.tour-card {
			animation: none;
		}
	}
</style>
