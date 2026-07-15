<script lang="ts">
	import { onMount } from 'svelte';
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import AppSidebar from '$lib/components/app-sidebar.svelte';
	import GeneratePanel from '$lib/components/generate-panel.svelte';
	import SiteHeader from '$lib/components/site-header.svelte';
	import StudioMetricsDashboard from '$lib/components/studio-metrics-dashboard.svelte';
	import StudioPreview from '$lib/components/studio-preview.svelte';
	import {
		loadHistory,
		loadModels,
		pollWhileWorking,
		studio,
		syncStarredIdsFromStorage
	} from '$lib/studio.svelte';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar';

	// The marketing site (Cloudflare Pages) is a static build with no API
	// behind it: PUBLIC_SITE_MODE=landing shows the canvas preview instead
	// of the studio. Product builds leave the variable empty.
	const landing = PUBLIC_SITE_MODE === 'landing';

	onMount(() => {
		if (landing) return;
		syncStarredIdsFromStorage();
		void loadModels();
		void loadHistory()
			.then(pollWhileWorking)
			.catch(() => {});
	});
</script>

<svelte:head>
	<title>potocolom - {t('app.title')}</title>
</svelte:head>

<div class="[--header-height:calc(var(--spacing)*14)]">
	<Sidebar.Provider class="flex h-svh flex-col overflow-hidden">
		<SiteHeader />
		<div class="flex min-h-0 flex-1">
			<AppSidebar />
			<Sidebar.Inset class="min-h-0 overflow-hidden">
				<div class="relative flex h-full min-h-0 flex-col p-4">
					{#if landing}
						<StudioPreview />
					{:else if studio.shellView === 'metrics'}
						<StudioMetricsDashboard />
					{:else}
						<GeneratePanel />
					{/if}
				</div>
			</Sidebar.Inset>
		</div>
	</Sidebar.Provider>
</div>
