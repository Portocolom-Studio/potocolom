<script lang="ts">
	import MailPlusIcon from '@lucide/svelte/icons/mail-plus';
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import StudioAdminAudit from '$lib/components/studio-admin-audit.svelte';
	import StudioAdminBanner from '$lib/components/studio-admin-banner.svelte';
	import StudioAdminFleet from '$lib/components/studio-admin-fleet.svelte';
	import StudioAdminInvitations from '$lib/components/studio-admin-invitations.svelte';
	import StudioAdminUsers from '$lib/components/studio-admin-users.svelte';
	import StudioHeaderTabs from '$lib/components/studio-header-tabs.svelte';
	import { openAdmin, studio } from '$lib/studio.svelte';
	import { useStudioHeader } from '$lib/studio-chrome.svelte';
	import type { AdminTab } from '$lib/studio-view';

	const tabs = $derived<{ value: AdminTab; label: string }[]>([
		{ value: 'users', label: t('app.admin.users') },
		{ value: 'invitations', label: t('app.admin.invitations') },
		{ value: 'fleet', label: t('app.admin.fleet') },
		{ value: 'audit', label: t('app.admin.audit') },
		{ value: 'banner', label: t('app.admin.banner') }
	]);
	const title = $derived(tabs.find((tab) => tab.value === studio.adminTab)?.label ?? '');
	const description = $derived(
		studio.adminTab === 'users'
			? t('app.admin.users_sub')
			: studio.adminTab === 'audit'
				? t('app.admin.audit_search_sub')
				: studio.adminTab === 'fleet'
					? t('app.admin.fleet_history_sub')
					: studio.adminTab === 'banner'
						? t('app.admin.banner_sub')
						: t('app.admin.invitations_sub')
	);

	useStudioHeader(() => ({ center: headerTabs, actions: headerActions }));
</script>

{#snippet headerTabs()}
	<StudioHeaderTabs
		label={t('app.admin.title')}
		{tabs}
		current={studio.adminTab}
		onSelect={(value) => openAdmin(value as AdminTab)}
	/>
{/snippet}

{#snippet headerActions()}
	{#if studio.adminTab !== 'invitations'}
		<Button size="sm" onclick={() => openAdmin('invitations')}>
			<MailPlusIcon />
			{t('app.admin.invite_someone')}
		</Button>
	{/if}
{/snippet}

<div class="flex h-full min-h-0 flex-col">
	<header class="px-4 pt-4 md:sr-only">
		<h1 class="text-xl font-semibold tracking-[-0.02em]">{title}</h1>
		<p class="text-muted-foreground mt-1 text-[12.5px]">{description}</p>
	</header>
	<nav class="flex flex-wrap gap-1 px-4 pt-3 md:hidden" aria-label={t('app.admin.title')}>
		{#each tabs as tab (tab.value)}
			<button
				type="button"
				class={'rounded-md px-2.5 py-1 text-xs font-medium ' +
					(studio.adminTab === tab.value ? 'bg-hover text-foreground' : 'text-muted-foreground')}
				aria-current={studio.adminTab === tab.value ? 'page' : undefined}
				onclick={() => openAdmin(tab.value)}>{tab.label}</button
			>
		{/each}
	</nav>
	<div class="min-h-0 flex-1">
		{#if studio.adminTab === 'users'}
			<StudioAdminUsers />
		{:else}
			<div class="no-scrollbar h-full overflow-y-auto px-4 py-5 md:px-6">
				{#if studio.adminTab === 'audit'}
					<StudioAdminAudit />
				{:else if studio.adminTab === 'fleet'}
					<StudioAdminFleet />
				{:else if studio.adminTab === 'banner'}
					<StudioAdminBanner />
				{:else}
					<StudioAdminInvitations />
				{/if}
			</div>
		{/if}
	</div>
</div>
