<script lang="ts">
	import type { Component } from 'svelte';
	import BoxIcon from '@lucide/svelte/icons/box';
	import ChartLineIcon from '@lucide/svelte/icons/chart-line';
	import HistoryIcon from '@lucide/svelte/icons/history';
	import LayersIcon from '@lucide/svelte/icons/layers';
	import LayoutGridIcon from '@lucide/svelte/icons/layout-grid';
	import LockIcon from '@lucide/svelte/icons/lock';
	import Maximize2Icon from '@lucide/svelte/icons/maximize-2';
	import MessageSquareTextIcon from '@lucide/svelte/icons/message-square-text';
	import PencilIcon from '@lucide/svelte/icons/pencil';
	import PenToolIcon from '@lucide/svelte/icons/pen-tool';
	import SparklesIcon from '@lucide/svelte/icons/sparkles';
	import StarIcon from '@lucide/svelte/icons/star';
	import UsersIcon from '@lucide/svelte/icons/users';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu/index.js';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { sectionNeeds, type GatedSection } from '$lib/account-display';
	import { account } from '$lib/account.svelte';
	import { prefetchView } from '$lib/studio-prefetch';
	import { t } from '$lib/i18n.svelte';
	import {
		openAdmin,
		openMetrics,
		openPlayground,
		openImages as openImagesView,
		openService,
		recentPrompts,
		studio
	} from '$lib/studio.svelte';

	// Every entry that changes the view goes through navigate, which closes
	// the mobile sheet (and drops its history entry) first.
	let { navigate = (run) => run() }: { navigate?: (run: () => unknown) => unknown } = $props();

	const sidebar = Sidebar.useSidebar();
	const role = $derived(account.current?.role ?? null);

	type CreateView = Extract<
		GatedSection,
		'generate' | 'image_to_image' | 'upscale' | 'realtime_canvas' | 'edit_image' | 'image_to_text'
	>;
	const createItems = $derived<
		{ view: CreateView; label: string; icon: Component; soon?: boolean }[]
	>([
		{ view: 'generate', label: t('app.service.generate'), icon: SparklesIcon },
		{ view: 'image_to_image', label: t('app.service.image_to_image'), icon: LayersIcon },
		{ view: 'upscale', label: t('app.service.upscale'), icon: Maximize2Icon },
		{ view: 'realtime_canvas', label: t('app.service.realtime_canvas'), icon: PenToolIcon },
		{ view: 'edit_image', label: t('app.service.edit_image'), icon: PencilIcon, soon: true },
		{
			view: 'image_to_text',
			label: t('app.service.image_to_text'),
			icon: MessageSquareTextIcon,
			soon: true
		}
	]);

	const workspaceNeeds = $derived(sectionNeeds('generate', role));
	const prompts = $derived(recentPrompts());

	const usageNeeds = $derived(sectionNeeds('metrics_usage', role));
	const benchmarksNeeds = $derived(sectionNeeds('metrics_benchmarks', role));
	const mineNeeds = $derived(sectionNeeds('metrics_mine', role));
	// Only a viewer is refused every tab, and the member gate is the one that
	// stops them first, so that is the reason a locked entry gives.
	const metricsLocked = $derived(
		usageNeeds !== null && benchmarksNeeds !== null && mineNeeds !== null
	);
	// A member opens on My usage: the other two tabs would be refused by the
	// URL the entry is about to build, and land them on generate.
	const metricsEntryTab = $derived(
		mineNeeds === null && usageNeeds !== null ? 'mine' : studio.metricsTab
	);
	const adminNeeds = $derived(sectionNeeds('admin', role));

	function lockReason(needs: 'user' | 'admin'): string {
		return needs === 'user' ? t('app.gen.members_only') : t('app.gen.admins_only');
	}

	function openImages(starred: boolean): void {
		openImagesView(starred);
	}

	function usePrompt(prompt: string): void {
		openPlayground();
		studio.prompt = prompt;
	}

	const itemClass =
		'h-[30px] gap-2.5 px-2 font-medium text-sidebar-foreground relative ' +
		'data-[active=true]:bg-sidebar-accent data-[active=true]:text-sidebar-accent-foreground ' +
		"data-[active=true]:before:content-[''] data-[active=true]:before:absolute data-[active=true]:before:-left-2 " +
		'data-[active=true]:before:top-[7px] data-[active=true]:before:bottom-[7px] data-[active=true]:before:w-0.5 ' +
		'data-[active=true]:before:rounded-full data-[active=true]:before:bg-brand ' +
		'group-data-[collapsible=icon]:size-9! group-data-[collapsible=icon]:p-2.5! ' +
		'group-data-[collapsible=icon]:data-[active=true]:before:hidden ' +
		'group-data-[collapsible=icon]:data-[active=true]:ring-1 group-data-[collapsible=icon]:data-[active=true]:ring-line-strong';
	const tailClass =
		'text-dim pointer-events-none absolute end-2 top-1/2 -translate-y-1/2 text-[11px] tabular-nums group-data-[collapsible=icon]:hidden';
</script>

{#snippet lock(needs: 'user' | 'admin')}
	<Tooltip.Root>
		<Tooltip.Trigger>
			{#snippet child({ props })}
				<span
					{...props}
					class="text-dim absolute end-2 top-1/2 grid size-4 -translate-y-1/2 place-items-center group-data-[collapsible=icon]:hidden"
					aria-hidden="true"
				>
					<LockIcon class="size-3.5" />
				</span>
			{/snippet}
		</Tooltip.Trigger>
		<Tooltip.Content side="right">{lockReason(needs)}</Tooltip.Content>
	</Tooltip.Root>
{/snippet}

<Sidebar.Group class="py-1">
	<Sidebar.GroupLabel class="text-dim h-7 px-2 text-[11px] font-medium tracking-[0.02em]">
		{t('app.shell.create')}
	</Sidebar.GroupLabel>
	<Sidebar.Menu class="gap-0">
		{#each createItems as item (item.view)}
			{@const needs = sectionNeeds(item.view, role)}
			<Sidebar.MenuItem>
				<Sidebar.MenuButton
					class={itemClass + (item.soon ? ' opacity-55' : '')}
					tooltipContent={item.label}
					isActive={studio.shellView === item.view}
				>
					{#snippet child({ props })}
						<button
							type="button"
							{...props}
							disabled={needs !== null}
							onclick={() => navigate(() => openService(item.view))}
						>
							<item.icon />
							<span>{item.label}</span>
							{#if needs !== null}
								<span class="sr-only">{lockReason(needs)}</span>
							{/if}
						</button>
					{/snippet}
				</Sidebar.MenuButton>
				{#if needs !== null}
					{@render lock(needs)}
				{:else if item.soon}
					<span
						class="border-line-strong text-dim pointer-events-none absolute end-2 top-1/2 -translate-y-1/2 rounded border px-1.5 text-[10.5px] leading-4 group-data-[collapsible=icon]:hidden"
					>
						{t('app.shell.soon')}
					</span>
				{:else if item.view === 'realtime_canvas' && studio.realtimeLive}
					<span
						class="bg-status-success pointer-events-none absolute end-3 top-1/2 size-[7px] -translate-y-1/2 rounded-full group-data-[collapsible=icon]:end-1.5 group-data-[collapsible=icon]:top-1.5 group-data-[collapsible=icon]:translate-y-0"
						aria-hidden="true"
					></span>
				{/if}
			</Sidebar.MenuItem>
		{/each}
	</Sidebar.Menu>
</Sidebar.Group>

<Sidebar.Separator class="mx-auto hidden w-6 group-data-[collapsible=icon]:block" />

<Sidebar.Group class="py-1">
	<Sidebar.GroupLabel class="text-dim h-7 px-2 text-[11px] font-medium tracking-[0.02em]">
		{t('app.shell.library')}
	</Sidebar.GroupLabel>
	<Sidebar.Menu class="gap-0">
		<Sidebar.MenuItem>
			<Sidebar.MenuButton
				class={itemClass}
				tooltipContent={t('app.images.title')}
				isActive={studio.shellView === 'images' && !studio.imagesStarredOnly}
				onclick={() => navigate(() => openImages(false))}
			>
				<LayoutGridIcon />
				<span>{t('app.images.title')}</span>
			</Sidebar.MenuButton>
		</Sidebar.MenuItem>
		<Sidebar.MenuItem>
			<Sidebar.MenuButton
				class={itemClass}
				tooltipContent={t('app.shell.starred')}
				isActive={studio.shellView === 'images' && studio.imagesStarredOnly}
				onclick={() => navigate(() => openImages(true))}
			>
				<StarIcon />
				<span>{t('app.shell.starred')}</span>
			</Sidebar.MenuButton>
			{#if studio.starredIds.length > 0}
				<span class={tailClass}>{studio.starredIds.length.toLocaleString()}</span>
			{/if}
		</Sidebar.MenuItem>
		<Sidebar.MenuItem>
			<DropdownMenu.Root>
				<DropdownMenu.Trigger disabled={workspaceNeeds !== null}>
					{#snippet child({ props })}
						<Sidebar.MenuButton
							{...props}
							class={itemClass}
							tooltipContent={t('app.shell.prompt_history')}
						>
							<HistoryIcon />
							<span>{t('app.shell.prompt_history')}</span>
							{#if workspaceNeeds !== null}
								<span class="sr-only">{lockReason(workspaceNeeds)}</span>
							{/if}
						</Sidebar.MenuButton>
					{/snippet}
				</DropdownMenu.Trigger>
				<DropdownMenu.Content
					class="w-80 max-w-[calc(100vw-2rem)]"
					side={sidebar.isMobile ? 'bottom' : 'right'}
					align="start"
					sideOffset={8}
				>
					<DropdownMenu.Label class="text-dim text-xs font-medium">
						{t('app.shell.prompt_history_hint')}
					</DropdownMenu.Label>
					{#each prompts as prompt (prompt)}
						<DropdownMenu.Item title={prompt} onclick={() => navigate(() => usePrompt(prompt))}>
							<span class="line-clamp-2 text-[13px]">{prompt}</span>
						</DropdownMenu.Item>
					{:else}
						<p class="text-muted-foreground px-2 py-1.5 text-[13px]">
							{t('app.shell.prompt_history_empty')}
						</p>
					{/each}
				</DropdownMenu.Content>
			</DropdownMenu.Root>
			{#if workspaceNeeds !== null}
				{@render lock(workspaceNeeds)}
			{/if}
		</Sidebar.MenuItem>
	</Sidebar.Menu>
</Sidebar.Group>

<Sidebar.Separator class="mx-auto hidden w-6 group-data-[collapsible=icon]:block" />

<Sidebar.Group class="py-1">
	<Sidebar.GroupLabel class="text-dim h-7 px-2 text-[11px] font-medium tracking-[0.02em]">
		{t('app.shell.platform')}
	</Sidebar.GroupLabel>
	<Sidebar.Menu class="gap-0">
		<Sidebar.MenuItem>
			<Sidebar.MenuButton
				class={itemClass}
				tooltipContent={t('app.models.title')}
				isActive={studio.shellView === 'models'}
				onclick={() => navigate(() => openService('models'))}
			>
				<BoxIcon />
				<span>{t('app.models.title')}</span>
			</Sidebar.MenuButton>
			{#if studio.models.length > 0}
				<span class={tailClass}>{studio.models.length}</span>
			{/if}
		</Sidebar.MenuItem>
		<Sidebar.MenuItem>
			<Sidebar.MenuButton
				class={itemClass}
				tooltipContent={t('app.shell.metrics')}
				isActive={studio.shellView === 'metrics'}
				aria-disabled={metricsLocked}
				onpointerenter={() => {
					if (!metricsLocked) prefetchView('metrics');
				}}
				onfocus={() => {
					if (!metricsLocked) prefetchView('metrics');
				}}
				onclick={() => {
					if (!metricsLocked) navigate(() => openMetrics(metricsEntryTab));
				}}
			>
				<ChartLineIcon />
				<span>{t('app.shell.metrics')}</span>
				{#if metricsLocked}
					<span class="sr-only">{t('app.gen.members_only')}</span>
				{/if}
			</Sidebar.MenuButton>
			{#if metricsLocked}
				{@render lock('user')}
			{/if}
		</Sidebar.MenuItem>
		{#if adminNeeds === null}
			<Sidebar.MenuItem>
				<Sidebar.MenuButton
					data-testid="admin-nav-entry"
					class={itemClass}
					tooltipContent={t('app.admin.title')}
					isActive={studio.shellView === 'admin'}
					onpointerenter={() => prefetchView('admin')}
					onfocus={() => prefetchView('admin')}
					onclick={() => navigate(() => openAdmin(studio.adminTab))}
				>
					<UsersIcon />
					<span>{t('app.admin.title')}</span>
				</Sidebar.MenuButton>
			</Sidebar.MenuItem>
		{/if}
	</Sidebar.Menu>
</Sidebar.Group>
