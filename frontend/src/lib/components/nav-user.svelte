<script lang="ts">
	import BadgeCheckIcon from '@lucide/svelte/icons/badge-check';
	import BellIcon from '@lucide/svelte/icons/bell';
	import ChevronsUpDownIcon from '@lucide/svelte/icons/chevrons-up-down';
	import CreditCardIcon from '@lucide/svelte/icons/credit-card';
	import LogOutIcon from '@lucide/svelte/icons/log-out';
	import SparklesIcon from '@lucide/svelte/icons/sparkles';

	import * as Avatar from '$lib/components/ui/avatar/index.js';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu/index.js';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { accountInitial, accountRoleLabelKey } from '$lib/account-display';
	import type { Account } from '$lib/account.svelte';
	import { t } from '$lib/i18n.svelte';

	let { account }: { account: Account } = $props();

	const sidebar = Sidebar.useSidebar();
	const initial = $derived(accountInitial(account.email));
	const roleLabel = $derived(t(accountRoleLabelKey(account.role)));
</script>

<Sidebar.Menu>
	<Sidebar.MenuItem>
		<DropdownMenu.Root>
			<DropdownMenu.Trigger>
				{#snippet child({ props })}
					<Sidebar.MenuButton size="lg" class="data-[state=open]:text-foreground" {...props}>
						<Avatar.Root class="size-8 rounded-lg">
							<Avatar.Fallback class="rounded-lg border border-border bg-transparent"
								>{initial}</Avatar.Fallback
							>
						</Avatar.Root>
						<div class="grid flex-1 text-start text-sm leading-tight">
							<span class="truncate font-medium">{account.email}</span>
							<span class="truncate text-xs">{roleLabel}</span>
						</div>
						<ChevronsUpDownIcon class="ms-auto size-4" />
					</Sidebar.MenuButton>
				{/snippet}
			</DropdownMenu.Trigger>
			<DropdownMenu.Content
				class="w-(--bits-dropdown-menu-anchor-width) min-w-56 rounded-lg"
				side={sidebar.isMobile ? 'bottom' : 'right'}
				align="end"
				sideOffset={4}
			>
				<DropdownMenu.Label class="p-0 font-normal">
					<div class="flex items-center gap-2 px-1 py-1.5 text-start text-sm">
						<Avatar.Root class="size-8 rounded-lg">
							<Avatar.Fallback class="rounded-lg border border-border bg-transparent"
								>{initial}</Avatar.Fallback
							>
						</Avatar.Root>
						<div class="grid flex-1 text-start text-sm leading-tight">
							<span class="truncate font-medium">{account.email}</span>
							<span class="truncate text-xs">{roleLabel}</span>
						</div>
					</div>
				</DropdownMenu.Label>
				<DropdownMenu.Separator />
				<DropdownMenu.Group>
					<DropdownMenu.Item>
						<SparklesIcon />
						{t('app.shell.upgrade_pro')}
					</DropdownMenu.Item>
				</DropdownMenu.Group>
				<DropdownMenu.Separator />
				<DropdownMenu.Group>
					<DropdownMenu.Item>
						<BadgeCheckIcon />
						{t('app.shell.account')}
					</DropdownMenu.Item>
					<DropdownMenu.Item>
						<CreditCardIcon />
						{t('app.shell.billing')}
					</DropdownMenu.Item>
					<DropdownMenu.Item>
						<BellIcon />
						{t('app.shell.notifications')}
					</DropdownMenu.Item>
				</DropdownMenu.Group>
				<DropdownMenu.Separator />
				<DropdownMenu.Item>
					<LogOutIcon />
					{t('app.shell.log_out')}
				</DropdownMenu.Item>
			</DropdownMenu.Content>
		</DropdownMenu.Root>
	</Sidebar.MenuItem>
</Sidebar.Menu>
