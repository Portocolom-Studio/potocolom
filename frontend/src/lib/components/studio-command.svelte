<script lang="ts">
	// Search or jump to (Cmd/Ctrl+K): every view, tab, text to image model and
	// recent prompt the signed-in role may open, from the keyboard. It only
	// calls the same open* functions the sidebar uses, so the URL routing and
	// role gates stay the single source of truth.
	import type { Component } from 'svelte';
	import { Command, Dialog } from 'bits-ui';
	import BoxIcon from '@lucide/svelte/icons/box';
	import ChartLineIcon from '@lucide/svelte/icons/chart-line';
	import HistoryIcon from '@lucide/svelte/icons/history';
	import LanguagesIcon from '@lucide/svelte/icons/languages';
	import LayersIcon from '@lucide/svelte/icons/layers';
	import LayoutGridIcon from '@lucide/svelte/icons/layout-grid';
	import Maximize2Icon from '@lucide/svelte/icons/maximize-2';
	import MessageSquareTextIcon from '@lucide/svelte/icons/message-square-text';
	import PencilIcon from '@lucide/svelte/icons/pencil';
	import PenToolIcon from '@lucide/svelte/icons/pen-tool';
	import SearchIcon from '@lucide/svelte/icons/search';
	import SparklesIcon from '@lucide/svelte/icons/sparkles';
	import StarIcon from '@lucide/svelte/icons/star';
	import UserIcon from '@lucide/svelte/icons/user';
	import UsersIcon from '@lucide/svelte/icons/users';
	import { sectionNeeds } from '$lib/account-display';
	import { account } from '$lib/account.svelte';
	import { getLocale, locales, setLocale, t, type Locale } from '$lib/i18n.svelte';
	import {
		filterTextToImageModels,
		openAccount,
		openAdmin,
		openMetrics,
		openPlayground,
		openImages,
		openService,
		recentPrompts,
		studio
	} from '$lib/studio.svelte';
	import { ADMIN_TABS } from '$lib/studio-view';
	import { studioChrome } from '$lib/studio-chrome.svelte';
	import { closeOnHistoryStep, popOverlay, pushOverlay } from '$lib/overlay-history';

	let {
		open = $bindable(false),
		navigate = (run) => run()
	}: {
		open?: boolean;
		/** Runs a chosen entry; the sidebar passes one that closes its mobile
		 * sheet first. */
		navigate?: (run: () => unknown) => unknown;
	} = $props();

	type Entry = {
		id: string;
		label: string;
		hint?: string;
		icon: Component;
		keywords?: string[];
		run: () => void;
	};

	const role = $derived(account.current?.role ?? null);
	const canCreate = $derived(sectionNeeds('generate', role) === null);

	const views = $derived.by<Entry[]>(() => {
		const entries: Entry[] = [];
		const create = [
			['generate', SparklesIcon],
			['image_to_image', LayersIcon],
			['upscale', Maximize2Icon],
			['realtime_canvas', PenToolIcon]
		] as const;
		for (const [view, icon] of create) {
			if (sectionNeeds(view, role) !== null) continue;
			entries.push({
				id: `view-${view}`,
				label: t(`app.service.${view}`),
				hint: t('app.shell.create'),
				icon,
				run: () => openService(view)
			});
		}
		// Not built yet, but they open a page of their own like any view.
		const soon = [
			['edit_image', PencilIcon],
			['image_to_text', MessageSquareTextIcon]
		] as const;
		for (const [view, icon] of soon) {
			if (sectionNeeds(view, role) !== null) continue;
			entries.push({
				id: `view-${view}`,
				label: t(`app.service.${view}`),
				hint: t('app.shell.soon'),
				icon,
				run: () => openService(view)
			});
		}
		entries.push(
			{
				id: 'view-images',
				label: t('app.images.title'),
				hint: t('app.shell.library'),
				icon: LayoutGridIcon,
				run: () => openImages(false)
			},
			{
				id: 'view-starred',
				label: t('app.shell.starred'),
				hint: t('app.shell.library'),
				icon: StarIcon,
				run: () => openImages(true)
			},
			{
				id: 'view-models',
				label: t('app.models.title'),
				hint: t('app.shell.platform'),
				icon: BoxIcon,
				run: () => openService('models')
			}
		);
		const metricsTabs = [
			['usage', 'metrics_usage', 'app.metrics.tab_usage'],
			['benchmarks', 'metrics_benchmarks', 'app.metrics.tab_benchmarks'],
			['mine', 'metrics_mine', 'app.metrics.mine_tab']
		] as const;
		for (const [tab, section, key] of metricsTabs) {
			if (sectionNeeds(section, role) !== null) continue;
			entries.push({
				id: `metrics-${tab}`,
				label: `${t('app.shell.metrics')}: ${t(key)}`,
				hint: t('app.shell.platform'),
				icon: ChartLineIcon,
				run: () => openMetrics(tab)
			});
		}
		if (sectionNeeds('admin', role) === null) {
			const labels = {
				users: 'app.admin.users',
				audit: 'app.admin.audit',
				fleet: 'app.admin.fleet',
				banner: 'app.admin.banner',
				invitations: 'app.admin.invitations'
			} as const;
			for (const tab of ADMIN_TABS) {
				entries.push({
					id: `admin-${tab}`,
					label: `${t('app.admin.title')}: ${t(labels[tab])}`,
					hint: t('app.shell.platform'),
					icon: UsersIcon,
					run: () => openAdmin(tab)
				});
			}
		}
		if (account.current) {
			entries.push({
				id: 'view-account',
				label: t('app.shell.account'),
				icon: UserIcon,
				run: openAccount
			});
		}
		return entries;
	});

	const models = $derived<Entry[]>(
		canCreate
			? filterTextToImageModels(studio.models).map((model) => ({
					id: `model-${model.id}`,
					label: model.name,
					hint: model.id,
					icon: BoxIcon,
					keywords: [model.id],
					run: () => {
						openPlayground();
						studio.modelId = model.id;
					}
				}))
			: []
	);

	const prompts = $derived<Entry[]>(
		canCreate
			? recentPrompts(8).map((prompt, index) => ({
					id: `prompt-${index}`,
					label: prompt,
					icon: HistoryIcon,
					run: () => {
						openPlayground();
						studio.prompt = prompt;
					}
				}))
			: []
	);

	// Whatever the open view offers, such as the canvas's Workbench and Focus.
	const viewActions = $derived<Entry[]>(studioChrome.commands);

	const languageNames: Record<Locale, string> = { en: 'English', es: 'Español' };
	const languages = $derived<Entry[]>(
		locales
			.filter((locale) => locale !== getLocale())
			.map((locale) => ({
				id: `locale-${locale}`,
				label: languageNames[locale],
				hint: t('ui.language'),
				icon: LanguagesIcon,
				run: () => setLocale(locale)
			}))
	);

	const groups = $derived(
		[
			{ id: 'view-actions', heading: t('app.shell.command_view_actions'), entries: viewActions },
			{ id: 'views', heading: t('app.shell.command_views'), entries: views },
			{ id: 'models', heading: t('app.models.title'), entries: models },
			{ id: 'prompts', heading: t('app.shell.prompt_history'), entries: prompts },
			{ id: 'language', heading: t('ui.language'), entries: languages }
		].filter((group) => group.entries.length > 0)
	);

	// Plain word matching rather than fuzzy letters: typing "met" should find
	// Metrics, not every entry that happens to contain an m, an e and a t.
	function matches(_value: string, search: string, keywords?: string[]): number {
		const haystack = (keywords ?? []).join(' ').toLowerCase();
		const words = search.toLowerCase().split(/\s+/).filter(Boolean);
		return words.every((word) => haystack.includes(word)) ? 1 : 0;
	}

	// Back closes the palette before it leaves the view. A choice drops the
	// palette's history entry first, so a choice that navigates lands after it
	// instead of being undone by it. A close by Back (or Forward) has already
	// left the palette's entry, so it must not step back again.
	let choosing = false;
	let closedByHistory = false;
	let wasOpen = false;
	$effect(() => {
		if (open && !wasOpen) pushOverlay('command');
		else if (!open && wasOpen && !choosing && !closedByHistory) void popOverlay('command');
		closedByHistory = false;
		wasOpen = open;
	});
	$effect(() =>
		closeOnHistoryStep(
			'command',
			() => open,
			() => {
				closedByHistory = true;
				open = false;
			}
		)
	);

	function choose(entry: Entry): void {
		choosing = true;
		open = false;
		void popOverlay('command').then(() => {
			choosing = false;
			void navigate(entry.run);
		});
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if (event.key.toLowerCase() !== 'k' || !(event.metaKey || event.ctrlKey)) return;
		if (event.altKey || event.shiftKey) return;
		event.preventDefault();
		open = !open;
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

<Dialog.Root bind:open>
	<Dialog.Portal>
		<Dialog.Overlay class="fixed inset-0 z-50 bg-black/60" />
		<Dialog.Content
			class="bg-popover text-popover-foreground border-line-strong fixed top-[15vh] left-1/2 z-50 w-[min(560px,calc(100vw-2rem))] -translate-x-1/2 overflow-hidden rounded-xl border shadow-[0_12px_40px_rgba(0,0,0,0.45)] outline-none"
		>
			<Dialog.Title class="sr-only">{t('app.shell.search')}</Dialog.Title>
			<Command.Root class="flex flex-col" loop filter={matches}>
				<div class="border-border flex items-center gap-2 border-b px-3">
					<SearchIcon class="text-dim size-4 shrink-0" aria-hidden="true" />
					<Command.Input
						class="placeholder:text-dim h-11 w-full bg-transparent text-[13px] outline-none"
						placeholder={t('app.shell.search_placeholder')}
						aria-label={t('app.shell.search')}
					/>
					<kbd
						class="border-line-strong text-dim rounded border border-b-2 px-1 font-sans text-[11px] leading-[15px]"
						>Esc</kbd
					>
				</div>
				<Command.List class="max-h-[min(420px,60vh)] overflow-y-auto p-1.5">
					<Command.Viewport>
						<Command.Empty class="text-muted-foreground px-3 py-6 text-center text-[13px]">
							{t('app.shell.search_empty')}
						</Command.Empty>
						{#each groups as group (group.id)}
							<Command.Group value={group.id}>
								<Command.GroupHeading class="text-dim px-2 pt-2 pb-1 text-[11px] font-medium">
									{group.heading}
								</Command.GroupHeading>
								<Command.GroupItems>
									{#each group.entries as entry (entry.id)}
										<Command.Item
											value={entry.id}
											keywords={[entry.label, ...(entry.keywords ?? [])]}
											onSelect={() => choose(entry)}
											class="data-selected:bg-hover data-selected:text-foreground text-muted-foreground flex h-8 cursor-default items-center gap-2.5 rounded-md px-2 text-[13px] outline-none"
										>
											<entry.icon class="size-4 shrink-0" aria-hidden="true" />
											<span class="text-foreground min-w-0 flex-1 truncate">{entry.label}</span>
											{#if entry.hint}
												<span class="text-dim shrink-0 truncate text-[11px]">{entry.hint}</span>
											{/if}
										</Command.Item>
									{/each}
								</Command.GroupItems>
							</Command.Group>
						{/each}
					</Command.Viewport>
				</Command.List>
			</Command.Root>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
