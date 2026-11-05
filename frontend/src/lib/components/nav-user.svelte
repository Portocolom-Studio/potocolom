<script lang="ts">
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { PUBLIC_SITE_MODE } from '$env/static/public';
	import BookOpenIcon from '@lucide/svelte/icons/book-open';
	import ChevronsUpDownIcon from '@lucide/svelte/icons/chevrons-up-down';
	import CircleQuestionMarkIcon from '@lucide/svelte/icons/circle-question-mark';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import LogOutIcon from '@lucide/svelte/icons/log-out';
	import MapIcon from '@lucide/svelte/icons/map';
	import UserIcon from '@lucide/svelte/icons/user';

	import * as Avatar from '$lib/components/ui/avatar/index.js';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu/index.js';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { apiFetch } from '$lib/api';
	import { accountInitial, accountRoleLabelKey, sectionNeeds } from '$lib/account-display';
	import { account as accountState, type Account } from '$lib/account.svelte';
	import { t } from '$lib/i18n.svelte';
	import { activateViewOwner, openAccount, studio } from '$lib/studio.svelte';

	// Without a signed-in account (AUTH_MODE=none, the landing preview) the
	// trigger is a plain Help row holding the same help entries.
	let {
		account,
		navigate = (run) => run()
	}: { account: Account | null; navigate?: (run: () => unknown) => unknown } = $props();

	const sidebar = Sidebar.useSidebar();
	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const initial = $derived(account ? accountInitial(account.email) : '');
	const roleLabel = $derived(account ? t(accountRoleLabelKey(account.role)) : '');
	// The landing preview renders this menu too, but it has no tour to replay.
	const canTour = $derived(
		PUBLIC_SITE_MODE !== 'landing' && sectionNeeds('generate', account?.role ?? null) === null
	);
	// The menu would hand focus back to its trigger after the tour has taken it.
	let tourChosen = false;

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
					{#if account}
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
								<span class="text-foreground truncate text-[12.5px] font-medium"
									>{account.email}</span
								>
								<span class="text-dim truncate text-[11px]">{roleLabel}</span>
							</div>
							<ChevronsUpDownIcon class="text-dim ms-auto size-3.5" />
						</Sidebar.MenuButton>
					{:else}
						<Sidebar.MenuButton
							class="data-[state=open]:bg-sidebar-accent text-sidebar-foreground h-9 gap-2.5 px-2 font-medium group-data-[collapsible=icon]:size-9! group-data-[collapsible=icon]:p-2.5!"
							tooltipContent={t('app.shell.help')}
							{...props}
						>
							<CircleQuestionMarkIcon />
							<span>{t('app.shell.help')}</span>
							<ChevronsUpDownIcon class="text-dim ms-auto size-3.5" />
						</Sidebar.MenuButton>
					{/if}
				{/snippet}
			</DropdownMenu.Trigger>
			<DropdownMenu.Content
				class="w-(--bits-dropdown-menu-anchor-width) min-w-56"
				side={sidebar.isMobile ? 'bottom' : 'right'}
				align="end"
				sideOffset={8}
				onCloseAutoFocus={(event) => {
					if (tourChosen) event.preventDefault();
					tourChosen = false;
				}}
			>
				{#if account}
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
					<DropdownMenu.Item onclick={() => navigate(openAccount)}>
						<UserIcon />
						{t('app.shell.account')}
					</DropdownMenu.Item>
				{/if}
				{#if canTour}
					<DropdownMenu.Item
						onclick={() => {
							tourChosen = true;
							void navigate(() => {
								studio.tourRequest = 'explicit';
							});
						}}
					>
						<MapIcon />
						{t('app.shell.show_tour')}
					</DropdownMenu.Item>
				{/if}
				<DropdownMenu.Item>
					{#snippet child({ props })}
						<a href={`${repoUrl}/tree/main/docs`} target="_blank" rel="noopener" {...props}>
							<BookOpenIcon />
							{t('app.shell.documentation')}
							<ExternalLinkIcon class="text-dim ms-auto size-3!" />
						</a>
					{/snippet}
				</DropdownMenu.Item>
				<DropdownMenu.Item>
					{#snippet child({ props })}
						<a href={`${repoUrl}/issues`} target="_blank" rel="noopener" {...props}>
							<CircleQuestionMarkIcon />
							{t('app.shell.support')}
							<ExternalLinkIcon class="text-dim ms-auto size-3!" />
						</a>
					{/snippet}
				</DropdownMenu.Item>
				{#if account}
					<DropdownMenu.Separator />
					<DropdownMenu.Item onclick={logOut}>
						<LogOutIcon />
						{t('app.shell.log_out')}
					</DropdownMenu.Item>
				{/if}
			</DropdownMenu.Content>
		</DropdownMenu.Root>
	</Sidebar.MenuItem>
	{#if logOutFailed}
		<p role="alert" class="text-destructive px-2 pt-1 text-xs">{t('app.shell.log_out_failed')}</p>
	{/if}
</Sidebar.Menu>
