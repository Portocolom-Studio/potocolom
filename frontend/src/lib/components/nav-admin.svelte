<script lang="ts">
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import CpuIcon from '@lucide/svelte/icons/cpu';
	import MegaphoneIcon from '@lucide/svelte/icons/megaphone';
	import ScrollTextIcon from '@lucide/svelte/icons/scroll-text';
	import UsersIcon from '@lucide/svelte/icons/users';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import * as Collapsible from '$lib/components/ui/collapsible/index.js';
	import { sectionNeeds } from '$lib/account-display';
	import { account } from '$lib/account.svelte';
	import { openAdmin, studio } from '$lib/studio.svelte';
	import { ADMIN_TABS } from '$lib/studio-view';

	const needs = $derived(sectionNeeds('admin', account.current?.role ?? null));
	const tabs = $derived([
		{ value: ADMIN_TABS[0], label: t('app.admin.users'), icon: UsersIcon },
		{ value: ADMIN_TABS[1], label: t('app.admin.audit'), icon: ScrollTextIcon },
		{ value: ADMIN_TABS[2], label: t('app.admin.fleet'), icon: CpuIcon },
		{ value: ADMIN_TABS[3], label: t('app.admin.banner'), icon: MegaphoneIcon }
	]);
</script>

{#if needs === null}
	<Collapsible.Root open class="group/collapsible">
		{#snippet child({ props })}
			<Sidebar.MenuItem {...props}>
				<Sidebar.MenuButton
					data-testid="admin-nav-entry"
					tooltipContent={t('app.admin.title')}
					isActive={studio.shellView === 'admin'}
					onclick={() => openAdmin(studio.adminTab)}
				>
					<UsersIcon />
					<span>{t('app.admin.title')}</span>
				</Sidebar.MenuButton>
				<Collapsible.Trigger>
					{#snippet child({ props: triggerProps })}
						<Sidebar.MenuAction {...triggerProps}>
							<ChevronRightIcon
								class="transition-transform group-data-[state=open]/collapsible:rotate-90"
							/>
							<span class="sr-only">{t('app.shell.toggle')} {t('app.admin.title')}</span>
						</Sidebar.MenuAction>
					{/snippet}
				</Collapsible.Trigger>
				<Collapsible.Content>
					<Sidebar.MenuSub>
						{#each tabs as tab (tab.value)}
							<Sidebar.MenuSubItem>
								<Sidebar.MenuSubButton
									isActive={studio.shellView === 'admin' && studio.adminTab === tab.value}
								>
									{#snippet child({ props: subProps })}
										<button type="button" {...subProps} onclick={() => openAdmin(tab.value)}>
											<tab.icon />
											<span>{tab.label}</span>
										</button>
									{/snippet}
								</Sidebar.MenuSubButton>
							</Sidebar.MenuSubItem>
						{/each}
					</Sidebar.MenuSub>
				</Collapsible.Content>
			</Sidebar.MenuItem>
		{/snippet}
	</Collapsible.Root>
{/if}
