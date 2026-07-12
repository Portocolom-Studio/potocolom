<script lang="ts">
	import type { ComponentProps } from 'svelte';
	import SquareTerminalIcon from '@lucide/svelte/icons/square-terminal';
	import BookOpenIcon from '@lucide/svelte/icons/book-open';
	import Settings2Icon from '@lucide/svelte/icons/settings-2';
	import LifeBuoyIcon from '@lucide/svelte/icons/life-buoy';
	import SendIcon from '@lucide/svelte/icons/send';
	import FrameIcon from '@lucide/svelte/icons/frame';
	import PieChartIcon from '@lucide/svelte/icons/pie-chart';
	import MapIcon from '@lucide/svelte/icons/map';
	import CommandIcon from '@lucide/svelte/icons/command';
	import { resolve } from '$app/paths';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import NavGallery from './nav-gallery.svelte';
	import NavHistory from './nav-history.svelte';
	import NavMain from './nav-main.svelte';
	import NavModels from './nav-models.svelte';
	import NavProjects from './nav-projects.svelte';
	import NavSecondary from './nav-secondary.svelte';
	import NavUser from './nav-user.svelte';

	let { ref = $bindable(null), ...restProps }: ComponentProps<typeof Sidebar.Root> = $props();

	const user = $derived({
		name: t('app.shell.user_name'),
		email: t('app.shell.user_email'),
		avatar: ''
	});

	const navMain = $derived([
		{
			title: t('app.shell.playground'),
			url: '#',
			icon: SquareTerminalIcon,
			isActive: true,
			items: [
				{ title: t('app.shell.starred'), url: '#' },
				{ title: t('app.shell.settings'), url: '#' }
			]
		},
		{
			title: t('app.shell.documentation'),
			url: '#',
			icon: BookOpenIcon,
			items: [
				{ title: t('app.shell.introduction'), url: '#' },
				{ title: t('app.shell.get_started'), url: '#' },
				{ title: t('app.shell.tutorials'), url: '#' },
				{ title: t('app.shell.changelog'), url: '#' }
			]
		},
		{
			title: t('app.shell.settings'),
			url: '#',
			icon: Settings2Icon,
			items: [
				{ title: t('app.shell.general'), url: '#' },
				{ title: t('app.shell.team'), url: '#' },
				{ title: t('app.shell.billing'), url: '#' },
				{ title: t('app.shell.limits'), url: '#' }
			]
		}
	]);

	const navSecondary = $derived([
		{ title: t('app.shell.support'), url: '#', icon: LifeBuoyIcon },
		{ title: t('app.shell.feedback'), url: '#', icon: SendIcon }
	]);

	const projects = $derived([
		{ name: t('app.shell.project_design'), url: '#', icon: FrameIcon },
		{ name: t('app.shell.project_sales'), url: '#', icon: PieChartIcon },
		{ name: t('app.shell.project_travel'), url: '#', icon: MapIcon }
	]);
</script>

<Sidebar.Root
	bind:ref
	class="top-(--header-height) !h-[calc(100svh-var(--header-height))]"
	{...restProps}
>
	<Sidebar.Header>
		<Sidebar.Menu>
			<Sidebar.MenuItem>
				<Sidebar.MenuButton size="lg">
					{#snippet child({ props })}
						<a href={resolve('/')} title={t('app.back')} {...props}>
							<div
								class="border-border text-foreground flex aspect-square size-8 items-center justify-center rounded-lg border bg-transparent"
							>
								<CommandIcon class="size-4" />
							</div>
							<div class="grid flex-1 text-start text-sm leading-tight">
								<span class="truncate font-medium">
									potocolom<span class="text-foreground">_</span>
								</span>
								<span class="truncate text-xs">{t('app.title')}</span>
							</div>
						</a>
					{/snippet}
				</Sidebar.MenuButton>
			</Sidebar.MenuItem>
		</Sidebar.Menu>
	</Sidebar.Header>
	<Sidebar.Content>
		<NavModels />
		<NavHistory />
		<NavGallery />
		<NavMain items={navMain} />
		<NavProjects {projects} />
		<NavSecondary items={navSecondary} class="mt-auto" />
	</Sidebar.Content>
	<Sidebar.Footer>
		<NavUser {user} />
	</Sidebar.Footer>
</Sidebar.Root>
