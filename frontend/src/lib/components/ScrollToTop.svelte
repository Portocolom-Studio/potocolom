<script lang="ts">
	import { browser } from '$app/environment';
	import ArrowUpIcon from '@lucide/svelte/icons/arrow-up';
	import { Button } from '$lib/components/ui/button';
	import { t } from '$lib/i18n.svelte';

	const thresholdPx = 200;

	let visible = $state(false);

	$effect(() => {
		if (!browser) return;

		function onScroll() {
			const distanceFromBottom =
				document.documentElement.scrollHeight - window.scrollY - window.innerHeight;
			visible = distanceFromBottom <= thresholdPx;
		}

		window.addEventListener('scroll', onScroll, { passive: true });
		onScroll();

		return () => window.removeEventListener('scroll', onScroll);
	});

	function scrollToTop() {
		window.scrollTo({
			top: 0,
			behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
		});
	}
</script>

{#if visible}
	<Button
		class="bg-background/90 fixed right-[calc(1rem+env(safe-area-inset-right))] bottom-[calc(1.5rem+env(safe-area-inset-bottom))] z-40 border shadow-md backdrop-blur-sm"
		aria-label={t('nav.scroll_to_top')}
		onclick={scrollToTop}
		size="icon"
		type="button"
		variant="outline"
	>
		<ArrowUpIcon />
	</Button>
{/if}
