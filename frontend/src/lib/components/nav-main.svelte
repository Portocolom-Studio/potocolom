<script lang="ts">
	import type { Component } from 'svelte';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import * as Collapsible from '$lib/components/ui/collapsible/index.js';

	let {
		items
	}: {
		items: {
			title: string;
			url: string;
			icon: Component;
			isActive?: boolean;
			items?: {
				title: string;
				url: string;
			}[];
		}[];
	} = $props();

	let openStates = $state<boolean[]>([]);

	$effect.pre(() => {
		if (openStates.length !== items.length) {
			openStates = items.map((item, index) => openStates[index] ?? item.isActive ?? false);
		}
	});
</script>

<Sidebar.Group>
	<Sidebar.GroupLabel>{t('app.shell.platform')}</Sidebar.GroupLabel>
	<Sidebar.Menu>
		{#each items as item, index (item.title)}
			{#if item.items?.length}
				<Collapsible.Root bind:open={openStates[index]} class="group/collapsible">
					{#snippet child({ props })}
						<Sidebar.MenuItem {...props}>
							<Collapsible.Trigger>
								{#snippet child({ props: triggerProps })}
									<Sidebar.MenuButton tooltipContent={item.title} {...triggerProps}>
										{#snippet child({ props: buttonProps })}
											<button type="button" {...buttonProps}>
												<item.icon />
												<span>{item.title}</span>
												<ChevronRightIcon
													class="ms-auto transition-transform group-data-[state=open]/collapsible:rotate-90"
												/>
											</button>
										{/snippet}
									</Sidebar.MenuButton>
								{/snippet}
							</Collapsible.Trigger>
							<Collapsible.Content>
								<Sidebar.MenuSub>
									{#each item.items as subItem (subItem.title)}
										<Sidebar.MenuSubItem>
											<Sidebar.MenuSubButton>
												{#snippet child({ props })}
													<a href={subItem.url} {...props}>
														<span>{subItem.title}</span>
													</a>
												{/snippet}
											</Sidebar.MenuSubButton>
										</Sidebar.MenuSubItem>
									{/each}
								</Sidebar.MenuSub>
							</Collapsible.Content>
						</Sidebar.MenuItem>
					{/snippet}
				</Collapsible.Root>
			{:else}
				<Sidebar.MenuItem>
					<Sidebar.MenuButton tooltipContent={item.title}>
						{#snippet child({ props })}
							<a href={item.url} {...props}>
								<item.icon />
								<span>{item.title}</span>
							</a>
						{/snippet}
					</Sidebar.MenuButton>
				</Sidebar.MenuItem>
			{/if}
		{/each}
	</Sidebar.Menu>
</Sidebar.Group>
