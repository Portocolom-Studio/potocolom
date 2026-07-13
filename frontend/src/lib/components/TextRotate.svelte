<script lang="ts">
	import { cn } from '$lib/utils';

	type RotateWord = {
		label: string;
		class?: string;
	};

	const transitionMs = 700;

	let {
		words,
		class: className,
		duration = 9
	}: {
		words: RotateWord[];
		class?: string;
		duration?: number;
	} = $props();

	let measureEl = $state<HTMLElement | null>(null);
	let innerEl = $state<HTMLElement | null>(null);
	let rowHeight = $state<number | undefined>(undefined);
	let minWidth = $state<number | undefined>(undefined);
	let index = $state(0);
	let animate = $state(true);
	let paused = $state(false);

	const stepMs = $derived((duration / Math.max(words.length, 1)) * 1000);
	const displayWords = $derived(words.length > 1 ? [...words, words[0]] : words);

	function measure() {
		if (!measureEl) return;
		const pills = [...measureEl.querySelectorAll('[data-measure]')];
		if (pills.length === 0) return;

		const widths = pills.map((element) => element.getBoundingClientRect().width);
		const heights = pills.map((element) => element.getBoundingClientRect().height);

		minWidth = Math.max(...widths);
		rowHeight = Math.max(...heights);
	}

	function onTransitionEnd(event: TransitionEvent) {
		if (event.target !== innerEl || event.propertyName !== 'transform') return;
		if (words.length > 1 && index === words.length) {
			animate = false;
			index = 0;
			requestAnimationFrame(() => {
				requestAnimationFrame(() => {
					animate = true;
				});
			});
		}
	}

	$effect(() => {
		words;
		index = 0;
		animate = true;
		measure();
	});

	$effect(() => {
		if (typeof ResizeObserver === 'undefined' || !measureEl) return;
		const observer = new ResizeObserver(() => measure());
		observer.observe(measureEl);
		return () => observer.disconnect();
	});

	$effect(() => {
		if (words.length < 2 || paused) return;
		const id = setInterval(() => {
			if (index < words.length) index += 1;
		}, stepMs);
		return () => clearInterval(id);
	});
</script>

<span
	class={cn('text-rotate', className, !rowHeight && 'text-rotate-measuring')}
	style:--row-height={rowHeight ? `${rowHeight}px` : undefined}
	style:min-width={minWidth ? `${minWidth}px` : undefined}
	aria-hidden="true"
	onmouseenter={() => (paused = true)}
	onmouseleave={() => (paused = false)}
>
	<span class="text-rotate-measure" bind:this={measureEl}>
		{#each words as word (word.label)}
			<span data-measure class={cn('text-rotate-pill', word.class)}>{word.label}</span>
		{/each}
	</span>

	<span
		bind:this={innerEl}
		class="text-rotate-inner"
		style:--row-height={rowHeight ? `${rowHeight}px` : undefined}
		style:transform={rowHeight ? `translateY(-${index * rowHeight}px)` : undefined}
		style:transition={animate
			? `transform ${transitionMs}ms cubic-bezier(0.45, 0.05, 0.55, 0.95)`
			: 'none'}
		ontransitionend={onTransitionEnd}
	>
		{#each displayWords as word, i (`${word.label}-${i}`)}
			<span class="text-rotate-row">
				<span class={cn('text-rotate-pill', word.class)}>{word.label}</span>
			</span>
		{/each}
	</span>
</span>

<style>
	.text-rotate {
		position: relative;
		display: inline-block;
		overflow: hidden;
		vertical-align: baseline;
		height: var(--row-height, 1lh);
		white-space: nowrap;
	}

	.text-rotate-measuring {
		visibility: hidden;
	}

	.text-rotate-measure {
		position: absolute;
		left: -9999px;
		top: 0;
		visibility: hidden;
		pointer-events: none;
		white-space: nowrap;
	}

	.text-rotate-inner {
		display: flex;
		flex-direction: column;
		will-change: transform;
	}

	.text-rotate-row {
		flex: 0 0 var(--row-height, 1lh);
		height: var(--row-height, 1lh);
		display: flex;
		align-items: center;
		overflow: hidden;
	}

	:global(.text-rotate-pill) {
		display: inline-block;
		white-space: nowrap;
		line-height: 1;
	}
</style>
