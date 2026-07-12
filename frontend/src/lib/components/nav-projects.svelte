<script lang="ts">
	import type { Component } from 'svelte';
	import FolderIcon from '@lucide/svelte/icons/folder';
	import EllipsisIcon from '@lucide/svelte/icons/ellipsis';
	import ShareIcon from '@lucide/svelte/icons/share';
	import Trash2Icon from '@lucide/svelte/icons/trash-2';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu/index.js';

	let {
		projects
	}: {
		projects: {
			name: string;
			url: string;
			icon: Component;
		}[];
	} = $props();

	const sidebar = Sidebar.useSidebar();
</script>

<Sidebar.Group class="group-data-[collapsible=icon]:hidden">
	<Sidebar.GroupLabel>{t('app.shell.projects')}</Sidebar.GroupLabel>
	<Sidebar.Menu>
		{#each projects as item (item.name)}
			<Sidebar.MenuItem>
				<Sidebar.MenuButton>
					{#snippet child({ props })}
						<a href={item.url} {...props}>
							<item.icon />
							<span>{item.name}</span>
						</a>
					{/snippet}
				</Sidebar.MenuButton>
				<DropdownMenu.Root>
					<DropdownMenu.Trigger>
						{#snippet child({ props })}
							<Sidebar.MenuAction showOnHover {...props}>
								<EllipsisIcon />
								<span class="sr-only">{t('app.shell.more')}</span>
							</Sidebar.MenuAction>
						{/snippet}
					</DropdownMenu.Trigger>
					<DropdownMenu.Content
						class="w-48"
						side={sidebar.isMobile ? 'bottom' : 'right'}
						align={sidebar.isMobile ? 'end' : 'start'}
					>
						<DropdownMenu.Item>
							<FolderIcon class="text-muted-foreground" />
							<span>{t('app.shell.view_project')}</span>
						</DropdownMenu.Item>
						<DropdownMenu.Item>
							<ShareIcon class="text-muted-foreground" />
							<span>{t('app.shell.share_project')}</span>
						</DropdownMenu.Item>
						<DropdownMenu.Separator />
						<DropdownMenu.Item>
							<Trash2Icon class="text-muted-foreground" />
							<span>{t('app.shell.delete_project')}</span>
						</DropdownMenu.Item>
					</DropdownMenu.Content>
				</DropdownMenu.Root>
			</Sidebar.MenuItem>
		{/each}
		<Sidebar.MenuItem>
			<Sidebar.MenuButton>
				<EllipsisIcon />
				<span>{t('app.shell.more')}</span>
			</Sidebar.MenuButton>
		</Sidebar.MenuItem>
	</Sidebar.Menu>
</Sidebar.Group>
