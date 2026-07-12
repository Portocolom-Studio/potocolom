<script lang="ts">
	import CollageMasonry from '$lib/components/collage/CollageMasonry.svelte';
	import { tick } from 'svelte';

	const maxHeightPx = 42 * 16;
	const maxHeightVh = 0.72;

	let viewportEl = $state<HTMLDivElement | null>(null);
	let contentEl = $state<HTMLDivElement | null>(null);
	let columnWidth = $state(120);
	let ready = $state(false);

	function maxViewportHeight() {
		if (typeof window === 'undefined') return maxHeightPx;
		return Math.min(window.innerHeight * maxHeightVh, maxHeightPx);
	}

	async function waitForImages(container: HTMLElement) {
		const images = [...container.querySelectorAll('img')];
		await Promise.all(
			images.map(
				(image) =>
					new Promise<void>((resolve) => {
						if (image.complete && image.naturalWidth > 0) {
							resolve();
							return;
						}
						image.addEventListener('load', () => resolve(), { once: true });
						image.addEventListener('error', () => resolve(), { once: true });
					})
			)
		);
	}

	async function fitContent() {
		if (!viewportEl || !contentEl) return;

		const viewportWidth = viewportEl.clientWidth;
		if (viewportWidth < 1) return;

		const heightCap = maxViewportHeight();
		const minWidth = viewportWidth < 640 ? 76 : 85;
		const maxWidth = viewportWidth < 640 ? 170 : 235;
		const step = 5;

		let chosenWidth = minWidth;

		for (let width = maxWidth; width >= minWidth; width -= step) {
			columnWidth = width;
			await tick();
			await waitForImages(contentEl);

			const height = contentEl.scrollHeight;
			if (height > 0 && height <= heightCap) {
				chosenWidth = width;
				ready = true;
				return;
			}

			chosenWidth = width;
		}

		columnWidth = chosenWidth;
		await tick();
		await waitForImages(contentEl);
		ready = true;
	}

	$effect(() => {
		if (!viewportEl || !contentEl) return;

		const content = contentEl;
		let cancelled = false;
		let fitting = false;

		const resize = () => {
			if (fitting) return;
			fitting = true;
			ready = false;
			void (async () => {
				if (!cancelled) await fitContent();
				fitting = false;
			})();
		};

		const observer = new ResizeObserver(resize);
		observer.observe(viewportEl);

		void (async () => {
			await waitForImages(content);
			if (!cancelled) await fitContent();
		})();

		return () => {
			cancelled = true;
			observer.disconnect();
		};
	});
</script>

<div
	bind:this={viewportEl}
	class="w-full overflow-hidden"
	class:min-h-[12rem]={!ready}
>
	<div
		bind:this={contentEl}
		class="w-full transition-opacity duration-200"
		class:opacity-0={!ready}
	>
		<CollageMasonry
			{columnWidth}
			columnGap="gap-1.5"
			tileMargin="mb-1.5"
			radius="rounded-lg"
			landing
			loading="eager"
		/>
	</div>
</div>
