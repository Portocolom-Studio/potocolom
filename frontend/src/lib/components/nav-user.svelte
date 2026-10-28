<script lang="ts">
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import ChevronsUpDownIcon from '@lucide/svelte/icons/chevrons-up-down';
	import LogOutIcon from '@lucide/svelte/icons/log-out';
	import UserIcon from '@lucide/svelte/icons/user';

	import * as Avatar from '$lib/components/ui/avatar/index.js';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu/index.js';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { apiFetch } from '$lib/api';
	import { accountInitial, accountRoleLabelKey } from '$lib/account-display';
	import { account as accountState, type Account } from '$lib/account.svelte';
	import { t } from '$lib/i18n.svelte';
	import { activateViewOwner, openAccount } from '$lib/studio.svelte';

	let {
		account,
		navigate = (run) => run()
	}: { account: Account; navigate?: (run: () => unknown) => unknown } = $props();

	const sidebar = Sidebar.useSidebar();
	const initial = $derived(accountInitial(account.email));
	const roleLabel = $derived(t(accountRoleLabelKey(account.role)));

	let logOutFailed = $state(false);

	// Only the server can end the session: its cookie is httpOnly. A failed
	// request leaves this browser signed in, so landing on /login would tell
	// someone on a shared computer they had left when they had not.
	async function logOut(): Promise<void> {
		logOutFailed = false;
		try {
			const response = await apiFetch('/api/v1/auth/logout', { method: 'POST' });
			if (!response.ok) {
				logOutFailed = true;
				return;
			}
		} catch {
			logOutFailed = true;
			return;
		}
		// The next account to sign in in this tab must not see this one's data.
		activateViewOwner(null, null);
		accountState.current = null;
		accountState.ownerId = null;
		accountState.localModeConfirmed = false;
		await navigate(() => goto(resolve('/login')));
	}
</script>

<Sidebar.Menu>
	<Sidebar.MenuItem>
		<DropdownMenu.Root>
			<DropdownMenu.Trigger>
				{#snippet child({ props })}
					<Sidebar.MenuButton
						size="lg"
						class="data-[state=open]:bg-sidebar-accent h-12 gap-2.5 px-1.5 group-data-[collapsible=icon]:size-9! group-data-[collapsible=icon]:p-0.5!"
						tooltipContent={account.email}
						{...props}
					>
						<Avatar.Root class="size-7 rounded-full">
							<Avatar.Fallback
								class="bg-hover text-foreground rounded-full text-xs font-medium ring-1 ring-[#333] ring-inset"
								>{initial}</Avatar.Fallback
							>
						</Avatar.Root>
						<div class="grid flex-1 text-start leading-tight">
							<span class="text-foreground truncate text-[12.5px] font-medium">{account.email}</span
							>
							<span class="text-dim truncate text-[11px]">{roleLabel}</span>
						</div>
						<ChevronsUpDownIcon class="text-dim ms-auto size-3.5" />
					</Sidebar.MenuButton>
				{/snippet}
			</DropdownMenu.Trigger>
			<DropdownMenu.Content
				class="w-(--bits-dropdown-menu-anchor-width) min-w-56"
				side={sidebar.isMobile ? 'bottom' : 'right'}
				align="end"
				sideOffset={8}
			>
				<DropdownMenu.Label class="p-0 font-normal">
					<div class="flex items-center gap-2 px-1 py-1.5 text-start">
						<Avatar.Root class="size-7 rounded-full">
							<Avatar.Fallback
								class="bg-hover text-foreground rounded-full text-xs font-medium ring-1 ring-[#333] ring-inset"
								>{initial}</Avatar.Fallback
							>
						</Avatar.Root>
						<div class="grid flex-1 text-start leading-tight">
							<span class="truncate text-[13px] font-medium">{account.email}</span>
							<span class="text-dim truncate text-xs">{roleLabel}</span>
						</div>
					</div>
				</DropdownMenu.Label>
				<DropdownMenu.Separator />
				{#if accountState.current}
					<!-- Hidden without a signed-in account: AUTH_MODE=none has none to open. -->
					<DropdownMenu.Item onclick={() => navigate(openAccount)}>
						<UserIcon />
						{t('app.shell.account')}
					</DropdownMenu.Item>
				{/if}
				<DropdownMenu.Item onclick={logOut}>
					<LogOutIcon />
					{t('app.shell.log_out')}
				</DropdownMenu.Item>
			</DropdownMenu.Content>
		</DropdownMenu.Root>
	</Sidebar.MenuItem>
	{#if logOutFailed}
		<p role="alert" class="text-destructive px-2 pt-1 text-xs">{t('app.shell.log_out_failed')}</p>
	{/if}
</Sidebar.Menu>
