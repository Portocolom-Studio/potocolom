<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import { account } from '$lib/account.svelte';
	import StudioAdminAudit from '$lib/components/studio-admin-audit.svelte';
	import StudioAdminBanner from '$lib/components/studio-admin-banner.svelte';
	import StudioAdminFleet from '$lib/components/studio-admin-fleet.svelte';
	import StudioAdminInvitations from '$lib/components/studio-admin-invitations.svelte';
	import StudioModelManagement from '$lib/components/studio-model-management.svelte';
	import StudioAdminUsers from '$lib/components/studio-admin-users.svelte';
	import StudioHeaderTabs from '$lib/components/studio-header-tabs.svelte';
	import { openAdmin, studio } from '$lib/studio.svelte';
	import { useStudioHeader } from '$lib/studio-chrome.svelte';
	import type { AdminTab } from '$lib/studio-view';

	const tabs = $derived<{ value: AdminTab; label: string }[]>([
		...(!account.localModeConfirmed
			? [
					{ value: 'users' as const, label: t('app.admin.users') },
					{ value: 'invitations' as const, label: t('app.admin.invitations') }
				]
			: []),
		{ value: 'fleet', label: t('app.admin.fleet') },
		{ value: 'models', label: t('app.model_management.title') },
		...(!account.localModeConfirmed
			? [
					{ value: 'audit' as const, label: t('app.admin.audit') },
					{ value: 'banner' as const, label: t('app.admin.banner') }
				]
			: [])
	]);
	$effect(() => {
		if (account.localModeConfirmed && studio.adminTab !== 'fleet' && studio.adminTab !== 'models')
			openAdmin('models');
	});
	const title = $derived(tabs.find((tab) => tab.value === studio.adminTab)?.label ?? '');
	useStudioHeader(() => ({ center: headerTabs }));
</script>

{#snippet headerTabs()}
	<StudioHeaderTabs
		label={t('app.admin.title')}
		{tabs}
		current={studio.adminTab}
		onSelect={(value) => openAdmin(value as AdminTab)}
	/>
{/snippet}

<div class="flex h-full min-h-0 flex-col">
	<h1 class="sr-only">{title}</h1>
	<nav
		class="no-scrollbar flex gap-1 overflow-x-auto border-b border-border px-4 py-3 [mask-image:linear-gradient(to_right,#000_calc(100%-1.5rem),transparent)] after:shrink-0 after:basis-4 after:content-[''] md:hidden"
		aria-label={t('app.admin.title')}
	>
		{#each tabs as tab (tab.value)}
			<button
				type="button"
				class={'shrink-0 whitespace-nowrap rounded-md px-2.5 py-1 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
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
			<div class="no-scrollbar h-full overflow-y-auto px-4 py-5 md:px-6" data-model-scroller>
				{#if studio.adminTab === 'audit'}
					<StudioAdminAudit />
				{:else if studio.adminTab === 'fleet'}
					<StudioAdminFleet />
				{:else if studio.adminTab === 'models'}
					<StudioModelManagement />
				{:else if studio.adminTab === 'banner'}
					<StudioAdminBanner />
				{:else}
					<StudioAdminInvitations />
				{/if}
			</div>
		{/if}
	</div>
</div>
