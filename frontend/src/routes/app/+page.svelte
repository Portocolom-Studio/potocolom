<script lang="ts">
	import AppSidebar from '$lib/components/app-sidebar.svelte';
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import SiteHeader from '$lib/components/site-header.svelte';
	import type { LatentCanvasApi } from '$lib/latent-canvas-scene';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar';

	let leftPanelEl = $state<HTMLDivElement | null>(null);
	let rightPanelEl = $state<HTMLDivElement | null>(null);
	let leftCanvasApi = $state<LatentCanvasApi | null>(null);
	let rightCanvasApi = $state<LatentCanvasApi | null>(null);

	function syncPreviewCursor(event: PointerEvent) {
		if (!leftPanelEl || !rightPanelEl) return;

		const leftRect = leftPanelEl.getBoundingClientRect();
		const rightRect = rightPanelEl.getBoundingClientRect();

		leftCanvasApi?.setCursor(event.clientX - leftRect.left, event.clientY - leftRect.top);
		rightCanvasApi?.setCursor(event.clientX - rightRect.left, event.clientY - rightRect.top);
	}

	function clearPreviewCursor() {
		leftCanvasApi?.setCursor(null, null);
		rightCanvasApi?.setCursor(null, null);
	}
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
					<div
						class="grid h-full min-h-0 flex-1 cursor-crosshair grid-cols-1 gap-4 md:grid-cols-2"
						role="presentation"
						onpointermove={syncPreviewCursor}
						onpointerleave={clearPreviewCursor}
					>
						<div
							bind:this={leftPanelEl}
							class="border-border/60 relative min-h-0 overflow-hidden rounded-xl border bg-[#070b14]"
						>
							<LatentCanvas
								animate
								followCursor
								seed={42}
								onAttach={(api) => (leftCanvasApi = api)}
							/>
							<span
								class="text-foreground/60 bg-background/55 pointer-events-none absolute inset-x-3 bottom-3 rounded-full px-3 py-1 text-center text-[0.65rem] tracking-[0.14em] uppercase backdrop-blur-sm"
							>
								{t('app.canvas_hint')}
							</span>
						</div>
						<div
							bind:this={rightPanelEl}
							class="border-border/60 relative min-h-0 overflow-hidden rounded-xl border bg-[#070b14]"
						>
							<LatentCanvas
								animate
								followCursor
								seed={137}
								onAttach={(api) => (rightCanvasApi = api)}
							/>
							<span
								class="text-foreground/60 bg-background/55 pointer-events-none absolute inset-x-3 bottom-3 rounded-full px-3 py-1 text-center text-[0.65rem] tracking-[0.14em] uppercase backdrop-blur-sm"
							>
								{t('app.result_hint')}
							</span>
						</div>
					</div>
					<p
						class="text-foreground/70 pointer-events-none absolute inset-x-6 bottom-5 text-center text-sm leading-relaxed"
					>
						{t('app.draw_placeholder')}
					</p>
				</div>
			</Sidebar.Inset>
		</div>
	</Sidebar.Provider>
</div>
