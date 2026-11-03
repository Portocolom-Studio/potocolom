<script lang="ts">
	import DocPage from './[tab]/[page]/+page.svelte';
	import type { PageData } from './[tab]/[page]/$types';

	let { data }: { data: PageData } = $props();
</script>

<DocPage {data} />
