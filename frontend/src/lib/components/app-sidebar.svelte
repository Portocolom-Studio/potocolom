<script lang="ts">
	import type { ComponentProps } from 'svelte';
	import BookOpenIcon from '@lucide/svelte/icons/book-open';
	import CircleQuestionMarkIcon from '@lucide/svelte/icons/circle-question-mark';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import PanelLeftIcon from '@lucide/svelte/icons/panel-left';
	import SearchIcon from '@lucide/svelte/icons/search';
	import { resolve } from '$app/paths';
	import { account } from '$lib/account.svelte';
	import { t } from '$lib/i18n.svelte';
	import { shortcutLabel } from '$lib/platform.svelte';
	import { closeOnHistoryStep, popOverlay, pushOverlay } from '$lib/overlay-history';
	import { Kbd } from '$lib/components/ui/kbd';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import NavStudio from './nav-studio.svelte';
	import NavUser from './nav-user.svelte';
	import StudioCommand from './studio-command.svelte';
	import StudioWorkerStatus from './studio-worker-status.svelte';

	let { ref = $bindable(null), ...restProps }: ComponentProps<typeof Sidebar.Root> = $props();

	const sidebar = Sidebar.useSidebar();
	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	let commandOpen = $state(false);

	// Back closes the mobile sidebar sheet before it leaves the view. A close
	// by Back (or Forward) has already left the sheet's entry, and a close
	// for a navigation drops it itself, so neither steps back again here.
	let sheetWasOpen = false;
	let sheetEntryHandled = false;
	$effect(() => {
		const open = sidebar.isMobile && sidebar.openMobile;
		if (open && !sheetWasOpen) pushOverlay('sidebar');
		else if (!open && sheetWasOpen && !sheetEntryHandled) void popOverlay('sidebar');
		if (!open) sheetEntryHandled = false;
		sheetWasOpen = open;
	});
	$effect(() =>
		closeOnHistoryStep(
			'sidebar',
			() => sidebar.openMobile,
			() => {
				if (sidebar.isMobile) sheetEntryHandled = true;
				sidebar.setOpenMobile(false);
			}
		)
	);

	/** Run a navigation chosen in the sidebar. With the mobile sheet open,
	 * the sheet closes and its history entry is dropped first, so the
	 * navigation lands after that step instead of on top of it. */
	async function navigateFromSidebar(run: () => unknown): Promise<void> {
		if (!(sidebar.isMobile && sidebar.openMobile)) {
			await run();
			return;
		}
		sheetEntryHandled = true;
		sidebar.setOpenMobile(false);
		await popOverlay('sidebar');
		await run();
	}
	// The shortcut hints follow the platform: Cmd on Apple keyboards, Ctrl
	// elsewhere. English is prerendered, so they settle after hydration.
	const shortcut = $derived(shortcutLabel(['mod', 'k']));
	const sidebarShortcut = $derived(shortcutLabel(['mod', 'b']));

	const footerItemClass =
		'h-[30px] gap-2.5 px-2 font-medium text-sidebar-foreground group-data-[collapsible=icon]:size-9! group-data-[collapsible=icon]:p-2.5!';
</script>

{#snippet searchTooltip()}
	{t('app.shell.search')}
	<Kbd>{shortcut}</Kbd>
{/snippet}

<Sidebar.Root bind:ref collapsible="icon" class="border-sidebar-border" {...restProps}>
	<Sidebar.Header
		class="border-sidebar-border h-[52px] flex-row items-center gap-2 border-b px-3.5 py-0 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
	>
		<a
			href={resolve('/')}
			title={t('app.back')}
			class="flex min-w-0 flex-1 items-baseline gap-2 rounded-md px-1.5 py-1 outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring group-data-[collapsible=icon]:hidden"
		>
			<span class="text-foreground font-[Lato] text-[15px] font-black tracking-[-0.03em]"
				>potocolom</span
			>
			<span class="text-dim text-[11.5px]">{t('app.title')}</span>
		</a>
		<button
			type="button"
			class="text-foreground hidden size-9 place-items-center rounded-md font-[Lato] text-lg font-black tracking-[-0.04em] outline-none hover:bg-sidebar-accent focus-visible:ring-2 focus-visible:ring-sidebar-ring group-data-[collapsible=icon]:grid"
			title={t('app.shell.expand_sidebar')}
			aria-label={t('app.shell.expand_sidebar')}
			onclick={() => sidebar.setOpen(true)}>p</button
		>
		<Tooltip.Root>
			<Tooltip.Trigger>
				{#snippet child({ props })}
					<button
						{...props}
						type="button"
						class="text-sidebar-foreground hover:bg-sidebar-accent hover:text-foreground hit-slop relative grid size-[30px] shrink-0 [--hit-slop-x:4px] place-items-center rounded-md outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring group-data-[collapsible=icon]:hidden"
						aria-keyshortcuts="Meta+B Control+B"
						onclick={sidebar.toggle}
					>
						<PanelLeftIcon class="size-4" />
						<span class="sr-only">{t('app.shell.toggle_sidebar')}</span>
					</button>
				{/snippet}
			</Tooltip.Trigger>
			<Tooltip.Content side="bottom">
				{t('app.shell.toggle_sidebar')}
				<Kbd>{sidebarShortcut}</Kbd>
			</Tooltip.Content>
		</Tooltip.Root>
	</Sidebar.Header>
	<div class="px-2 pt-2.5 pb-0.5 group-data-[collapsible=icon]:px-2.5">
		<Sidebar.MenuButton
			class="border-line-strong text-dim hover:text-muted-foreground h-[30px] gap-2 rounded-md border bg-transparent px-2.5 font-normal group-data-[collapsible=icon]:size-9! group-data-[collapsible=icon]:border-0 group-data-[collapsible=icon]:p-2.5!"
			tooltipContent={searchTooltip}
			aria-keyshortcuts="Meta+K Control+K"
			onclick={() => (commandOpen = true)}
		>
			<SearchIcon class="size-3.5!" />
			<span class="flex-1 text-start group-data-[collapsible=icon]:hidden"
				>{t('app.shell.search_placeholder')}</span
			>
			<kbd
				class="border-line-strong text-dim rounded border border-b-2 px-1 font-sans text-[11px] leading-[15px] group-data-[collapsible=icon]:hidden"
				>{shortcut}</kbd
			>
		</Sidebar.MenuButton>
	</div>
	<Sidebar.Content class="gap-0 pt-1">
		<NavStudio navigate={navigateFromSidebar} />
	</Sidebar.Content>
	<Sidebar.Footer class="gap-0 p-0">
		<StudioWorkerStatus />
		<Sidebar.Menu class="gap-0 px-2 pt-1 pb-1.5">
			<Sidebar.MenuItem>
				<Sidebar.MenuButton class={footerItemClass} tooltipContent={t('app.shell.documentation')}>
					{#snippet child({ props })}
						<a href={`${repoUrl}/tree/main/docs`} target="_blank" rel="noopener" {...props}>
							<BookOpenIcon />
							<span class="flex-none!">{t('app.shell.documentation')}</span>
							<ExternalLinkIcon class="size-3! group-data-[collapsible=icon]:hidden" />
						</a>
					{/snippet}
				</Sidebar.MenuButton>
			</Sidebar.MenuItem>
			<Sidebar.MenuItem>
				<Sidebar.MenuButton class={footerItemClass} tooltipContent={t('app.shell.support')}>
					{#snippet child({ props })}
						<a href={`${repoUrl}/issues`} target="_blank" rel="noopener" {...props}>
							<CircleQuestionMarkIcon />
							<span>{t('app.shell.support')}</span>
						</a>
					{/snippet}
				</Sidebar.MenuButton>
			</Sidebar.MenuItem>
		</Sidebar.Menu>
		{#if account.current}
			<div class="border-sidebar-border border-t px-2 py-1.5">
				<NavUser account={account.current} navigate={navigateFromSidebar} />
			</div>
		{/if}
	</Sidebar.Footer>
</Sidebar.Root>

<StudioCommand bind:open={commandOpen} navigate={navigateFromSidebar} />
