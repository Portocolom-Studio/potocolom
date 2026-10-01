<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import StudioAdminAudit from '$lib/components/studio-admin-audit.svelte';
	import StudioAdminFleet from '$lib/components/studio-admin-fleet.svelte';
	import StudioAdminUsers from '$lib/components/studio-admin-users.svelte';
	import { studio } from '$lib/studio.svelte';

	const title = $derived(
		studio.adminTab === 'users'
			? t('app.admin.users')
			: studio.adminTab === 'audit'
				? t('app.admin.audit')
				: t('app.admin.fleet')
	);
	const description = $derived(
		studio.adminTab === 'users'
			? t('app.admin.users_sub')
			: studio.adminTab === 'audit'
				? t('app.admin.audit_search_sub')
				: t('app.admin.fleet_history_sub')
	);
</script>

<div class="flex h-full min-h-0 flex-col gap-4">
	<header class="shrink-0">
		<p class="text-muted-foreground text-sm">{t('app.admin.title')}</p>
		<h1 class="text-2xl font-semibold tracking-tight">{title}</h1>
		<p class="text-muted-foreground mt-1 text-sm">{description}</p>
	</header>
	<div class="min-h-0 flex-1">
		{#if studio.adminTab === 'users'}
			<StudioAdminUsers />
		{:else if studio.adminTab === 'audit'}
			<StudioAdminAudit />
		{:else}
			<StudioAdminFleet />
		{/if}
	</div>
</div>
