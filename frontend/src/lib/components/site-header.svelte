<script lang="ts">
	import PanelLeftIcon from '@lucide/svelte/icons/panel-left';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { account } from '$lib/account.svelte';
	import { t } from '$lib/i18n.svelte';
	import { studio } from '$lib/studio.svelte';
	import { studioChrome } from '$lib/studio-chrome.svelte';

	const sidebar = Sidebar.useSidebar();

	// Where the view sits in the sidebar's groups: the first crumb names the
	// group, the second the view.
	const group = $derived.by(() => {
		switch (studio.shellView) {
			case 'images':
				return t('app.shell.library');
			case 'models':
			case 'metrics':
			case 'admin':
				return t('app.shell.platform');
			case 'account':
				return account.current?.email ?? t('app.title');
			default:
				return t('app.shell.create');
		}
	});

	const sectionTitle = $derived(
		studio.shellView === 'metrics'
			? t('app.metrics.title')
			: studio.shellView === 'admin'
				? t('app.admin.title')
				: studio.shellView === 'images'
					? studio.imagesStarredOnly
						? t('app.shell.starred')
						: t('app.images.title')
					: studio.shellView === 'models'
						? t('app.models.title')
						: t(`app.service.${studio.shellView}`)
	);
</script>

<header
	class="bg-background border-border relative z-20 flex h-[52px] w-full shrink-0 items-center gap-3 border-b px-4"
>
	{#if sidebar.isMobile}
		<button
			type="button"
			class="text-muted-foreground hover:bg-hover hit-slop relative grid size-[30px] shrink-0 place-items-center rounded-md"
			aria-keyshortcuts="Meta+B Control+B"
			onclick={sidebar.toggle}
		>
			<PanelLeftIcon class="size-4" />
			<span class="sr-only">{t('app.shell.toggle_sidebar')}</span>
		</button>
	{/if}
	<nav aria-label={t('app.shell.breadcrumb')} class="min-w-0 shrink-0">
		<ol class="text-muted-foreground flex items-center gap-1.5 font-medium whitespace-nowrap">
			<li class="hidden max-w-48 truncate sm:block">{group}</li>
			<li class="text-dim hidden sm:block" aria-hidden="true">/</li>
			<li class="text-foreground truncate font-semibold" aria-current="page">{sectionTitle}</li>
		</ol>
	</nav>
	{#if studioChrome.center}
		<div class="flex h-full min-w-0 items-stretch">
			{@render studioChrome.center()}
		</div>
	{/if}
	<div class="flex-1"></div>
	{#if studioChrome.actions}
		<div class="flex min-w-0 shrink-0 items-center gap-2">
			{@render studioChrome.actions()}
		</div>
	{/if}
</header>
