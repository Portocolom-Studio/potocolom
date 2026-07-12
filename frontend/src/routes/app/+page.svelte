<script lang="ts">
	import AppSidebar from '$lib/components/app-sidebar.svelte';
	import GeneratePanel from '$lib/components/generate-panel.svelte';
	import SiteHeader from '$lib/components/site-header.svelte';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar';
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
					<GeneratePanel />
				</div>
			</Sidebar.Inset>
		</div>
	</Sidebar.Provider>
</div>
