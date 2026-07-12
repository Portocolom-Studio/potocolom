<script lang="ts">
	import SearchIcon from '@lucide/svelte/icons/search';
	import { Label } from '$lib/components/ui/label/index.js';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { t } from '$lib/i18n.svelte';
	import type { HTMLFormAttributes } from 'svelte/elements';
	import type { WithElementRef } from '$lib/utils.js';

	let { ref = $bindable(null), ...restProps }: WithElementRef<HTMLFormAttributes> = $props();
</script>

<form {...restProps} bind:this={ref}>
	<div class="relative">
		<Label for="search" class="sr-only">{t('app.shell.search_label')}</Label>
		<Sidebar.Input
			id="search"
			placeholder={t('app.shell.search_placeholder')}
			class="h-8 ps-7"
		/>
		<SearchIcon
			class="pointer-events-none absolute start-2 top-1/2 size-4 -translate-y-1/2 opacity-50 select-none"
		/>
	</div>
</form>
