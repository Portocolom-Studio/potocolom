<script lang="ts">
	import { goto, onNavigate } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import { updated } from '$app/stores';
	import { onDestroy, onMount } from 'svelte';
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import { account, type Account } from '$lib/account.svelte';
	import { openViewFor } from '$lib/account-display';
	import { probeAccount, type AccountProbeResult } from '$lib/account-probe';
	import { accountCheckForcesLogin, loginSearchFor } from '$lib/auth-flow';
	import AppSidebar from '$lib/components/app-sidebar.svelte';
	import GeneratePanel from '$lib/components/generate-panel.svelte';
	import LineageCanvas from '$lib/components/lineage-canvas.svelte';
	import ModelPanel from '$lib/components/model-panel.svelte';
	import RealtimeCanvasPanel from '$lib/components/realtime-canvas-panel.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import ServiceSketchPanel from '$lib/components/service-sketch-panel.svelte';
	import SiteHeader from '$lib/components/site-header.svelte';
	import StatusBanner from '$lib/components/status-banner.svelte';
	import StudioAccount from '$lib/components/studio-account.svelte';
	import StudioPreview from '$lib/components/studio-preview.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import { Toaster } from '$lib/components/ui/sonner';
	import {
		loadHistory,
		loadModels,
		loadStarredGenerations,
		migrateStoredFavorites,
		pollWhileWorking,
		startGenerationUpdates,
		stopGenerationUpdates,
		activateViewOwner,
		resetViewOwner,
		viewOwnerEpoch,
		studio
	} from '$lib/studio.svelte';
	import { readStudioView } from '$lib/studio-view';
	import { t } from '$lib/i18n.svelte';
	import { detectPlatform } from '$lib/platform.svelte';
	import { revalidationDue } from '$lib/swr-cache';
	import { loadLazyView } from '$lib/studio-prefetch';
	import { startStudioViewTransition, viewTransitionsEnabled } from '$lib/view-transition';
	import * as Sidebar from '$lib/components/ui/sidebar';

	// The marketing site (Cloudflare Pages) is a static build with no API
	// behind it: PUBLIC_SITE_MODE=landing shows the canvas preview instead
	// of the studio. Product builds leave the variable empty.
	const landing = PUBLIC_SITE_MODE === 'landing';
	$effect.pre(() => {
		const owner = account.ownerId
			? { kind: 'account' as const, id: account.ownerId }
			: account.localModeConfirmed
				? { kind: 'local' as const }
				: null;
		activateViewOwner(owner, account.current?.role ?? null);
	});
	// pre, so the role's answer changes the view before the studio renders: a
	// plain effect lets a refused panel mount once and fire its requests.
	$effect.pre(() => {
		viewOwnerEpoch();
		const { view, tab } = readStudioView(page.url);
		studio.shellView = openViewFor(view, tab, account.current?.role ?? null);
		// Only the active tabbed view applies its URL tab, and only when it was
		// not refused, so other views keep their last tab open.
		if (
			studio.shellView === 'metrics' &&
			(tab === 'usage' || tab === 'benchmarks' || tab === 'mine')
		) {
			studio.metricsTab = tab;
		}
		if (
			studio.shellView === 'admin' &&
			(tab === 'users' ||
				tab === 'audit' ||
				tab === 'fleet' ||
				tab === 'banner' ||
				tab === 'invitations')
		) {
			studio.adminTab = tab;
		}
	});
	let updateDismissed = $state(false);
	const updateAvailable = $derived(!landing && $updated && !updateDismissed);

	// Set on teardown: the preload chain can resolve after unmount, and starting
	// the update loop then would run streams with no UI mounted.
	let cancelled = false;
	let lastPreloadedEpoch: number | null = null;
	let managedPreloadEpoch: number | null = null;
	let startupProbeController: AbortController | null = null;
	let focusProbeController: AbortController | null = null;

	// Pending until the account probe answers, so a signed-out visitor never
	// sees the studio shell. Landing mode skips the probe entirely.
	let checkingAccount = $state(!landing);

	$effect(() => {
		const epoch = viewOwnerEpoch();
		if (checkingAccount || cancelled || (!account.ownerId && !account.localModeConfirmed)) return;
		preloadStudioOwner(epoch);
	});

	onMount(() => {
		detectPlatform();
		if (landing) return;
		void guardStudioEntry();
	});

	async function guardStudioEntry() {
		account.current = null;
		account.ownerId = null;
		account.localModeConfirmed = false;
		resetViewOwner();
		const controller = new AbortController();
		startupProbeController = controller;
		const result = await probeAccount(controller.signal);
		if (startupProbeController === controller) startupProbeController = null;
		if (cancelled) return;
		applyAccountProbeResult(result);
		if (result.kind === 'signed_out' && accountCheckForcesLogin(401)) {
			// replaceState so Back from the sign-in page does not bounce here.
			await goto(`${resolve('/login')}${loginSearchFor(page.url.search)}`, { replaceState: true });
			return;
		}
		checkingAccount = false;
		lastFocusRefresh = Date.now();
		if (result.kind === 'failed' || result.kind === 'account' || result.kind === 'local') {
			preloadStudioOwner(viewOwnerEpoch());
		}
	}

	function preloadStudioOwner(preloadEpoch: number): void {
		if (cancelled || preloadEpoch !== viewOwnerEpoch() || lastPreloadedEpoch === preloadEpoch)
			return;
		lastPreloadedEpoch = preloadEpoch;
		managedPreloadEpoch = preloadEpoch;
		void loadModels();
		// History first so it paints without waiting on the one-time favorites
		// migration, which stars each stored id in turn. Favorites then load once;
		// later history refreshes reconcile locally instead of re-fetching the list.
		void loadHistory()
			.then(() => (preloadEpoch === viewOwnerEpoch() ? migrateStoredFavorites() : undefined))
			.then(() => (preloadEpoch === viewOwnerEpoch() ? loadStarredGenerations() : undefined))
			.then(() => {
				if (cancelled || preloadEpoch !== viewOwnerEpoch()) return;
				return startGenerationUpdates();
			})
			.catch(() => {
				// Best-effort preload: the panel shows its empty states and the
				// poll loop recovers once the API answers.
			});
	}

	// Coming back to the tab checks the account first, then refreshes models and
	// the newest history page for the same confirmed owner. This catches work
	// started in another tab without reloading or clearing the warm view.
	const FOCUS_REFRESH_MS = 30_000;
	let lastFocusRefresh: number | null = null;
	let focusProbeInFlight = false;

	function applyAccountProbeResult(result: AccountProbeResult): void {
		let current: Account | null = null;
		let ownerId: string | null = null;
		let localModeConfirmed = false;
		if (result.kind === 'account') {
			current = result.current;
			ownerId = result.ownerId;
		} else if (result.kind === 'unknown') {
			current = result.current;
		} else if (result.kind === 'local') {
			localModeConfirmed = true;
		}
		account.current = current;
		account.ownerId = ownerId;
		account.localModeConfirmed = localModeConfirmed;
		activateViewOwner(
			ownerId ? { kind: 'account', id: ownerId } : localModeConfirmed ? { kind: 'local' } : null,
			current?.role ?? null
		);
	}

	async function refreshOnFocus(): Promise<void> {
		if (landing || checkingAccount || cancelled || lastFocusRefresh === null || focusProbeInFlight)
			return;
		if (document.visibilityState !== 'visible') return;
		const now = Date.now();
		if (!revalidationDue(lastFocusRefresh, now, FOCUS_REFRESH_MS)) return;
		lastFocusRefresh = now;
		focusProbeInFlight = true;
		const beforeEpoch = viewOwnerEpoch();
		const controller = new AbortController();
		focusProbeController = controller;
		let result: AccountProbeResult;
		try {
			result = await probeAccount(controller.signal);
		} finally {
			focusProbeInFlight = false;
			if (focusProbeController === controller) focusProbeController = null;
		}
		if (cancelled || controller.signal.aborted || viewOwnerEpoch() !== beforeEpoch) return;
		if (result.kind === 'failed') return;
		applyAccountProbeResult(result);
		if (result.kind === 'signed_out') {
			if (accountCheckForcesLogin(401)) {
				await goto(`${resolve('/login')}${loginSearchFor(page.url.search)}`, {
					replaceState: true
				});
			}
			return;
		}
		if (viewOwnerEpoch() !== beforeEpoch) return;
		if (!account.ownerId && !account.localModeConfirmed) return;
		void loadModels();
		void loadHistory()
			.then(() => pollWhileWorking())
			.catch(() => {
				// The poll loop and the next focus try again.
			});
	}

	// A view switch is a navigation within /app; crossfade it. Leaving the
	// studio (sign in, the landing page) is not animated.
	onNavigate((navigation) => {
		if (!viewTransitionsEnabled()) return;
		if (navigation.from?.url.pathname !== navigation.to?.url.pathname) return;
		if (navigation.from?.url.search === navigation.to?.url.search) return;
		// The page takes no input while a transition runs, and the canvas is
		// drawn on the moment it appears (and often left and returned to in a
		// moment): crossfading into or out of it would swallow strokes.
		if (
			(navigation.from && readStudioView(navigation.from.url).view === 'realtime_canvas') ||
			(navigation.to && readStudioView(navigation.to.url).view === 'realtime_canvas')
		)
			return;
		if (
			navigation.from &&
			navigation.to &&
			readStudioView(navigation.from.url).view === 'metrics' &&
			readStudioView(navigation.to.url).view === 'metrics'
		)
			return;
		return new Promise((resolve) => {
			startStudioViewTransition(async () => {
				resolve();
				// A newer navigation can abort this one; the transition then
				// just ends, and the newer one is not animated twice.
				await navigation.complete.catch(() => {});
			});
		});
	});

	onDestroy(() => {
		cancelled = true;
		startupProbeController?.abort();
		focusProbeController?.abort();
		if (managedPreloadEpoch === viewOwnerEpoch()) stopGenerationUpdates();
	});
</script>

<!-- The shape of a dashboard while its code loads: a row of stat cards and a
     panel. The text stays for screen readers. -->
{#snippet viewSkeleton(label: string)}
	<div role="status" class="flex flex-col gap-3 px-4 py-5 md:px-6">
		<span class="sr-only">{label}</span>
		<div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-hidden="true">
			{#each [0, 1, 2, 3] as index (index)}
				<Skeleton class="h-[86px] rounded-[10px]" />
			{/each}
		</div>
		<Skeleton class="h-72 rounded-[10px]" aria-hidden="true" />
	</div>
{/snippet}

<svelte:window onfocus={refreshOnFocus} />
<svelte:document onvisibilitychange={refreshOnFocus} />

<Seo
	titleKey={landing ? 'seo.app_landing_title' : 'seo.app_title'}
	descriptionKey={landing ? 'seo.app_landing_description' : 'seo.app_description'}
	path="/app"
/>

<div data-studio-shell class="contents">
	{#if landing}
		<h1 class="sr-only">potocolom realtime generative image studio preview</h1>
	{/if}
	{#if landing || !checkingAccount}
		<!-- viewport-fit=cover lets the page reach under a notch and the home
		     indicator; the safe area insets keep the shell's edges, the top bar,
		     the history strip and the canvas's floating bars, clear of them. -->
		<Sidebar.Provider
			class="isolate h-svh overflow-hidden pt-[env(safe-area-inset-top)] pe-[env(safe-area-inset-right)] pb-[env(safe-area-inset-bottom)] ps-[env(safe-area-inset-left)]"
			style="--sidebar-width: 240px; --sidebar-width-icon: 56px;"
		>
			<AppSidebar />
			<Sidebar.Inset class="flex min-h-0 min-w-0 flex-col overflow-hidden">
				<SiteHeader />
				{#if !landing}
					<StatusBanner />
				{/if}
				{#if studio.favoriteNotice || updateAvailable}
					<div
						class="bg-raised border-border flex items-center gap-2 border-b px-4 py-2 text-[13px]"
					>
						<!-- The live region is the text only: a region containing focusable
						     controls makes screen reader announcements confusing. Flex with a
						     gap, so the messages are spaced by layout rather than by template
						     whitespace. -->
						<span role="status" aria-live="polite" class="flex flex-1 flex-wrap gap-x-1">
							{#if studio.favoriteNotice}<span>{studio.favoriteNotice}</span>{/if}
							{#if updateAvailable}<span>{t('app.update.available')}</span>{/if}
						</span>
						{#if updateAvailable}
							<Button size="sm" onclick={() => location.reload()}>
								{t('app.update.reload')}
							</Button>
							<Button variant="ghost" size="sm" onclick={() => (updateDismissed = true)}>
								{t('app.update.dismiss')}
							</Button>
						{/if}
					</div>
				{/if}
				<div class="relative flex min-h-0 flex-1 flex-col">
					{#key studio.viewOwnerEpoch}
						{#if landing}
							<div class="flex h-full min-h-0 flex-col p-4">
								<StudioPreview />
							</div>
						{:else if studio.shellView === 'metrics'}
							<!-- The chart stack (layerchart, d3) is the largest JS in the studio;
						loading it with the view keeps it out of every visitor's first paint. -->
							{#await loadLazyView('metrics')}
								{@render viewSkeleton(t('app.metrics.loading'))}
							{:then { default: MetricsDashboard }}
								<MetricsDashboard />
							{:catch}
								<p class="text-muted-foreground p-6 text-sm">{t('app.metrics.load_failed')}</p>
							{/await}
						{:else if studio.shellView === 'admin'}
							{#await loadLazyView('admin')}
								{@render viewSkeleton(t('app.admin.loading'))}
							{:then { default: AdminDashboard }}
								<AdminDashboard />
							{:catch}
								<p class="text-muted-foreground p-6 text-sm">{t('app.admin.load_failed')}</p>
							{/await}
						{:else if studio.shellView === 'account'}
							<StudioAccount />
						{:else if studio.shellView === 'models'}
							<ModelPanel />
						{:else if studio.shellView === 'images'}
							<LineageCanvas />
						{:else if studio.shellView === 'realtime_canvas'}
							<RealtimeCanvasPanel />
						{:else if studio.shellView === 'edit_image' || studio.shellView === 'image_to_text'}
							<div class="flex h-full min-h-0 flex-col p-4">
								<ServiceSketchPanel mode={studio.shellView} />
							</div>
						{:else}
							<GeneratePanel mode={studio.shellView} />
						{/if}
					{/key}
				</div>
			</Sidebar.Inset>
		</Sidebar.Provider>
		<!-- Undo offers and quick failures. Offsets clear the home indicator and
		     rounded corners on phones with a notch. -->
		<Toaster
			position="bottom-right"
			containerAriaLabel={t('app.toast.region')}
			closeButtonAriaLabel={t('app.toast.close')}
			offset={{
				bottom: 'calc(24px + env(safe-area-inset-bottom))',
				right: 'calc(24px + env(safe-area-inset-right))'
			}}
			mobileOffset={{
				bottom: 'calc(16px + env(safe-area-inset-bottom))',
				left: 'calc(16px + env(safe-area-inset-left))',
				right: 'calc(16px + env(safe-area-inset-right))'
			}}
		/>
	{:else}
		<p class="text-muted-foreground flex h-svh items-center justify-center text-sm">
			{t('app.loading')}
		</p>
	{/if}
</div>
