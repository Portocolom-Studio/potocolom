<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import AppSidebar from '$lib/components/app-sidebar.svelte';
	import GeneratePanel from '$lib/components/generate-panel.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import SiteHeader from '$lib/components/site-header.svelte';
	import StudioMetricsDashboard from '$lib/components/studio-metrics-dashboard.svelte';
	import StudioPreview from '$lib/components/studio-preview.svelte';
	import {
		loadHistory,
		loadModels,
		loadStarredGenerations,
		migrateStoredFavorites,
		pollWhileWorking,
		stopGenerationUpdates,
		studio
	} from '$lib/studio.svelte';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar';

	// The marketing site (Cloudflare Pages) is a static build with no API
	// behind it: PUBLIC_SITE_MODE=landing shows the canvas preview instead
	// of the studio. Product builds leave the variable empty.
	const landing = PUBLIC_SITE_MODE === 'landing';

	onMount(() => {
		if (landing) return;
		void loadModels();
		// History first so it paints without waiting on the one-time favorites
		// migration, which stars each stored id in turn. Favorites then load once;
		// later history refreshes reconcile locally instead of re-fetching the list.
		void loadHistory()
			.then(migrateStoredFavorites)
			.then(loadStarredGenerations)
			.then(pollWhileWorking)
			.catch(() => {
				// Best-effort preload: the panel shows its empty states and the
				// poll loop recovers once the API answers.
			});
	});

	onDestroy(stopGenerationUpdates);
</script>

<Seo
	title={landing
		? 'potocolom Studio Preview | Realtime AI Canvas'
		: `potocolom - ${t('app.title')}`}
	description={landing
		? 'Explore the static potocolom studio preview for its pre-alpha realtime generative image workflow. The managed cloud waitlist has not opened.'
		: 'Open the potocolom studio on your connected self-hosted deployment.'}
	path="/app"
/>

<div class="[--header-height:calc(var(--spacing)*14)]">
	{#if landing}
		<h1 class="sr-only">potocolom realtime generative image studio preview</h1>
	{/if}
	<Sidebar.Provider class="flex h-svh flex-col overflow-hidden">
		<SiteHeader />
		<div class="flex min-h-0 flex-1">
			<AppSidebar />
			<Sidebar.Inset class="min-h-0 overflow-hidden">
				<div class="relative flex h-full min-h-0 flex-col p-4">
					{#if studio.favoriteNotice}
						<p role="status" aria-live="polite" class="bg-muted mb-3 rounded-md px-3 py-2 text-sm">
							{studio.favoriteNotice}
						</p>
					{/if}
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
