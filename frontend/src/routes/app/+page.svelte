<script lang="ts">
	import AppSidebar from '$lib/components/app-sidebar.svelte';
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
					<div class="grid h-full min-h-0 flex-1 grid-cols-1 gap-4 md:grid-cols-2">
						<div class="border-border/60 relative min-h-0 rounded-xl border border-dashed">
							<span
								class="text-muted-foreground absolute inset-0 grid place-items-center px-6 text-center text-xs tracking-[0.14em] uppercase"
							>
								{t('app.canvas_hint')}
							</span>
						</div>
						<div class="border-border/60 relative min-h-0 rounded-xl border border-dashed">
							<span
								class="text-muted-foreground absolute inset-0 grid place-items-center px-6 text-center text-xs tracking-[0.14em] uppercase"
							>
								{t('app.result_hint')}
							</span>
						</div>
					</div>
					<p
						class="text-muted-foreground pointer-events-none absolute inset-x-6 bottom-5 text-center text-sm leading-relaxed"
					>
						{t('app.draw_placeholder')}
					</p>
				</div>
			</Sidebar.Inset>
		</div>
	</Sidebar.Provider>
</div>
