<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import {
		adminErrorMessage,
		buildAdminConfirmation,
		buildUsersQuery,
		needsAdminAttestation,
		recheckAdminAccess
	} from '$lib/studio-admin-logic';
	import { formatDateTime, t } from '$lib/i18n.svelte';
	import SearchIcon from '@lucide/svelte/icons/search';
	import { Button } from '$lib/components/ui/button';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import { accountInitial } from '$lib/account-display';
	import { Dialog } from 'bits-ui';
	import type { Role } from '$lib/account-display';
	import type { Generation } from '$lib/studio.svelte';

	type AdminUser = {
		id: string;
		email: string;
		role: Role;
		state: 'active' | 'suspended' | 'disabled' | 'deletion_pending' | 'purging';
		created_at: string | null;
		mail_verified: boolean;
		generations?: number;
		assets?: number;
	};

	type EditableState = 'active' | 'suspended' | 'disabled' | 'deletion_pending';
	type UserAction =
		{ kind: 'role'; value: Role } | { kind: 'state'; value: EditableState } | { kind: 'restore' };
	type Confirmation = {
		userId: string;
		userEmail: string;
		userMailVerified: boolean;
		action: UserAction;
	};

	let users = $state<AdminUser[]>([]);
	let nextCursor = $state<string | null>(null);
	let searchQuery = $state('');
	let loadingMore = $state(false);
	let loadMoreError = $state('');
	let searchReady = false;
	let usersLoadEpoch = 0;
	let selectedId = $state<string | null>(null);
	let detail = $state<AdminUser | null>(null);
	let generations = $state<Generation[]>([]);
	let usersLoading = $state(true);
	let detailLoading = $state(false);
	let usersError = $state('');
	let detailError = $state('');
	let actionError = $state('');
	let selectedRole = $state<Role>('user');
	let selectedState = $state<EditableState>('active');
	let pendingConfirmation = $state<Confirmation | null>(null);
	let confirmationOpen = $state(false);
	let attestIdentity = $state(false);
	let actionSubmitting = $state(false);
	let selectionEpoch = 0;
	const confirmationNeedsAttestation = $derived.by(() => {
		if (!pendingConfirmation || pendingConfirmation.action.kind !== 'role') return false;
		return needsAdminAttestation(
			pendingConfirmation.action.value,
			pendingConfirmation.userMailVerified
		);
	});

	const confirmationMessage = $derived.by(() => {
		if (!pendingConfirmation) return '';
		const action = pendingConfirmation.action;
		let change = t('app.admin.confirm_restore');
		if (action.kind === 'role') {
			change = t('app.admin.confirm_role').replace('{role}', t(roleLabelKey(action.value)));
		} else if (action.kind === 'state') {
			change = t('app.admin.confirm_state').replace('{state}', t(stateLabelKey(action.value)));
		}
		return buildAdminConfirmation(
			t('app.admin.confirmation_text'),
			pendingConfirmation.userEmail,
			change
		);
	});

	function roleLabelKey(
		role: Role
	): 'app.admin.role_admin' | 'app.admin.role_viewer' | 'app.admin.role_user' {
		if (role === 'admin') return 'app.admin.role_admin';
		if (role === 'viewer') return 'app.admin.role_viewer';
		return 'app.admin.role_user';
	}

	function stateLabelKey(
		state: AdminUser['state'] | EditableState
	):
		| 'app.admin.state_active'
		| 'app.admin.state_suspended'
		| 'app.admin.state_disabled'
		| 'app.admin.state_deletion_pending'
		| 'app.admin.state_purging' {
		if (state === 'suspended') return 'app.admin.state_suspended';
		if (state === 'disabled') return 'app.admin.state_disabled';
		if (state === 'deletion_pending') return 'app.admin.state_deletion_pending';
		if (state === 'purging') return 'app.admin.state_purging';
		return 'app.admin.state_active';
	}

	function stateTone(state: AdminUser['state']): string {
		if (state === 'active') return 'tag-success';
		if (state === 'suspended') return 'tag-warning';
		if (state === 'deletion_pending' || state === 'purging') return 'tag-danger';
		return '';
	}

	async function apiError(response: Response): Promise<string> {
		if (response.status === 403) void recheckAdminAccess();
		const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
		return adminErrorMessage(body?.detail, response.statusText);
	}

	async function loadUsers(
		options: { cursor?: string | null; append?: boolean } = {}
	): Promise<void> {
		const { cursor = null, append = false } = options;
		const epoch = ++usersLoadEpoch;
		loadMoreError = '';
		if (append) loadingMore = true;
		else {
			usersLoading = true;
			usersError = '';
		}
		try {
			const response = await apiFetch(`/api/v1/users${buildUsersQuery(searchQuery, cursor)}`);
			if (epoch !== usersLoadEpoch) return;
			if (!response.ok) {
				// The cursor is the last loaded account, and a purge can delete it:
				// start the list over rather than strand the rows already shown.
				if (append && response.status === 404) {
					void loadUsers();
					return;
				}
				const message = await apiError(response);
				if (epoch !== usersLoadEpoch) return;
				if (append) loadMoreError = message;
				else {
					usersError = message;
					users = [];
				}
				return;
			}
			const body = (await response.json()) as { users: AdminUser[]; next_cursor: string | null };
			if (epoch !== usersLoadEpoch) return;
			users = append ? [...users, ...body.users] : body.users;
			nextCursor = body.next_cursor;
		} catch {
			if (epoch !== usersLoadEpoch) return;
			if (append) loadMoreError = t('app.admin.request_failed');
			else usersError = t('app.admin.request_failed');
		} finally {
			if (epoch === usersLoadEpoch) {
				usersLoading = false;
				loadingMore = false;
			}
		}
	}

	function loadMoreUsers(): void {
		if (nextCursor === null || loadingMore) return;
		void loadUsers({ cursor: nextCursor, append: true });
	}

	async function selectUser(userId: string): Promise<void> {
		selectedId = userId;
		await loadSelectedUser(userId);
	}

	async function loadSelectedUser(userId: string): Promise<void> {
		const epoch = ++selectionEpoch;
		detail = null;
		generations = [];
		detailLoading = true;
		detailError = '';
		actionError = '';
		try {
			const [detailResponse, generationsResponse] = await Promise.all([
				apiFetch(`/api/v1/users/${encodeURIComponent(userId)}`),
				apiFetch(`/api/v1/users/${encodeURIComponent(userId)}/generations?limit=50`)
			]);
			if (epoch !== selectionEpoch) return;
			if (!detailResponse.ok) {
				detailError = await apiError(detailResponse);
				detail = null;
				generations = [];
				return;
			}
			detail = (await detailResponse.json()) as AdminUser;
			selectedRole = detail.role;
			selectedState = detail.state === 'purging' ? 'active' : detail.state;
			if (!generationsResponse.ok) {
				detailError = await apiError(generationsResponse);
				generations = [];
				return;
			}
			generations = (await generationsResponse.json()) as Generation[];
		} catch {
			if (epoch === selectionEpoch) detailError = t('app.admin.request_failed');
		} finally {
			if (epoch === selectionEpoch) detailLoading = false;
		}
	}

	function requestAction(user: AdminUser, action: UserAction): void {
		actionError = '';
		attestIdentity = false;
		pendingConfirmation = {
			userId: user.id,
			userEmail: user.email,
			userMailVerified: user.mail_verified,
			action
		};
		confirmationOpen = true;
	}

	function requestRoleChange(): void {
		if (!detail || selectedRole === detail.role) return;
		requestAction(detail, { kind: 'role', value: selectedRole });
	}

	function requestStateChange(): void {
		if (!detail || selectedState === detail.state) return;
		requestAction(detail, { kind: 'state', value: selectedState });
	}

	function cancelConfirmation(): void {
		confirmationOpen = false;
		pendingConfirmation = null;
		attestIdentity = false;
		actionError = '';
	}

	async function confirmAction(): Promise<void> {
		const confirmation = pendingConfirmation;
		if (!confirmation || actionSubmitting) return;
		actionSubmitting = true;
		actionError = '';
		try {
			const path = `/api/v1/users/${encodeURIComponent(confirmation.userId)}`;
			let response: Response;
			if (confirmation.action.kind === 'role') {
				response = await apiFetch(`${path}/role`, {
					method: 'POST',
					headers: { 'content-type': 'application/json' },
					body: JSON.stringify({
						role: confirmation.action.value,
						attested:
							needsAdminAttestation(confirmation.action.value, confirmation.userMailVerified) &&
							attestIdentity
					})
				});
			} else if (confirmation.action.kind === 'state') {
				response = await apiFetch(`${path}/state`, {
					method: 'POST',
					headers: { 'content-type': 'application/json' },
					body: JSON.stringify({ state: confirmation.action.value })
				});
			} else {
				response = await apiFetch(`${path}/restore`, { method: 'POST' });
			}
			if (!response.ok) {
				actionError = await apiError(response);
				return;
			}
			confirmationOpen = false;
			pendingConfirmation = null;
			attestIdentity = false;
			await loadUsers();
			await loadSelectedUser(confirmation.userId);
		} catch {
			actionError = t('app.admin.request_failed');
		} finally {
			actionSubmitting = false;
		}
	}

	onMount(() => void loadUsers());

	// Debounced 300ms: a search firing on every keystroke would race itself
	// and spam the backend. searchReady skips the run the effect fires on
	// mount, which onMount's own load already covers.
	$effect(() => {
		const query = searchQuery;
		if (!searchReady) {
			searchReady = true;
			return;
		}
		void query;
		const timer = setTimeout(() => void loadUsers(), 300);
		return () => clearTimeout(timer);
	});
</script>

<div class="flex h-full min-h-0 flex-col overflow-y-auto xl:flex-row xl:overflow-hidden">
	<section class="flex min-h-0 min-w-0 flex-1 flex-col gap-3 p-4 md:p-5 xl:overflow-y-auto">
		<h2 class="sr-only">{t('app.admin.users_title')}</h2>
		<div class="flex flex-wrap items-center gap-3">
			<label
				class="bg-field border-line-strong focus-within:ring-ring/60 flex h-8 w-full max-w-[300px] items-center gap-2 rounded-md border px-2.5 focus-within:ring-2"
			>
				<SearchIcon class="text-dim size-3.5 shrink-0" aria-hidden="true" />
				<input
					type="search"
					name="q"
					autocomplete="off"
					spellcheck={false}
					data-testid="admin-user-search"
					class="placeholder:text-dim min-w-0 flex-1 bg-transparent text-[13px] outline-none"
					aria-label={t('app.admin.search_users')}
					placeholder={t('app.admin.search_users')}
					bind:value={searchQuery}
				/>
			</label>
			<span class="flex-1"></span>
			<span data-testid="admin-user-count" class="text-dim text-xs tabular-nums">
				{t('app.admin.users_count').replace('{count}', String(users.length))}
			</span>
		</div>
		<div class="bg-card border-border overflow-x-auto rounded-[10px] border">
			{#if usersLoading}
				<div role="status">
					<span class="sr-only">{t('app.admin.loading_users')}</span>
					<div class="min-w-[560px]" aria-hidden="true">
						<div class="border-border flex h-[34px] items-center gap-6 border-b px-3">
							<Skeleton class="h-2.5 w-16 rounded" />
							<Skeleton class="h-2.5 w-10 rounded" />
							<Skeleton class="h-2.5 w-10 rounded" />
						</div>
						{#each [0, 1, 2, 3, 4, 5] as index (index)}
							<div class="border-border flex h-11 items-center gap-2.5 px-3 not-first:border-t">
								<Skeleton class="size-6 rounded-md" />
								<Skeleton class="h-3 w-48 rounded" />
								<span class="flex-1"></span>
								<Skeleton class="h-5 w-16 rounded-full" />
								<Skeleton class="h-3 w-24 rounded" />
							</div>
						{/each}
					</div>
				</div>
			{:else if usersError}
				<p role="alert" class="text-destructive p-4 text-[13px]">{usersError}</p>
			{:else if users.length === 0}
				<p class="text-muted-foreground p-4 text-[13px]">{t('app.admin.users_empty')}</p>
			{:else}
				<table class="w-full min-w-[560px] border-collapse text-[12.5px]">
					<thead>
						<tr class="text-muted-foreground border-border border-b text-xs">
							<th class="h-[34px] px-3 text-start font-medium">{t('app.admin.col_account')}</th>
							<th class="h-[34px] px-3 text-start font-medium">{t('app.admin.role')}</th>
							<th class="h-[34px] px-3 text-start font-medium">{t('app.admin.state')}</th>
							<th class="h-[34px] px-3 text-start font-medium">{t('app.admin.generations')}</th>
							<th class="h-[34px] px-3 text-start font-medium">{t('app.admin.created')}</th>
						</tr>
					</thead>
					<tbody>
						{#each users as user (user.id)}
							<tr
								class={'border-border border-b last:border-b-0 ' +
									(selectedId === user.id ? 'bg-[#141414]' : 'hover:bg-[#121212]')}
							>
								<td class="h-11 px-3">
									<button
										type="button"
										data-testid="admin-user-row"
										class="flex max-w-full items-center gap-2.5 rounded text-start outline-none focus-visible:ring-2 focus-visible:ring-ring"
										aria-pressed={selectedId === user.id}
										onclick={() => selectUser(user.id)}
									>
										<span
											class="ring-line-strong bg-raised grid size-6 shrink-0 place-items-center rounded-md text-[11px] font-medium ring-1 ring-inset"
											aria-hidden="true">{accountInitial(user.email)}</span
										>
										<span class="truncate">{user.email}</span>
									</button>
								</td>
								<td class="text-muted-foreground px-3">{t(roleLabelKey(user.role))}</td>
								<td class="px-3">
									<span class={'tag ' + stateTone(user.state)}>{t(stateLabelKey(user.state))}</span>
								</td>
								<td class="px-3 tabular-nums">{user.generations ?? '-'}</td>
								<td class="text-muted-foreground px-3 whitespace-nowrap"
									>{formatDateTime(user.created_at)}</td
								>
							</tr>
						{/each}
					</tbody>
				</table>
				{#if nextCursor !== null}
					<div class="border-border border-t px-3 py-2.5">
						<Button
							variant="ghost"
							size="sm"
							data-testid="admin-users-load-more"
							disabled={loadingMore}
							onclick={loadMoreUsers}
						>
							{loadingMore ? t('app.admin.loading_users') : t('app.admin.load_more')}
						</Button>
						{#if loadMoreError}
							<p role="alert" class="text-destructive mt-2 text-[13px]">{loadMoreError}</p>
						{/if}
					</div>
				{/if}
			{/if}
		</div>
	</section>

	<aside
		class="bg-panel border-border min-h-0 shrink-0 border-t xl:w-[360px] xl:overflow-y-auto xl:border-t-0 xl:border-s"
		aria-label={t('app.admin.user_detail')}
	>
		{#if detailLoading && !detail}
			<div role="status">
				<span class="sr-only">{t('app.admin.loading_user')}</span>
				<div aria-hidden="true">
					<div class="border-border flex items-center gap-3 border-b px-4 py-4">
						<Skeleton class="size-10 rounded-lg" />
						<div class="flex flex-1 flex-col gap-2">
							<Skeleton class="h-3 w-40 rounded" />
							<Skeleton class="h-2.5 w-28 rounded" />
						</div>
					</div>
					<div class="flex flex-col gap-3 p-4">
						{#each [0, 1, 2] as index (index)}
							<Skeleton class="h-9 w-full rounded-md" />
						{/each}
					</div>
				</div>
			</div>
		{:else if detailError && !detail}
			<p role="alert" class="text-destructive p-5 text-[13px]">{detailError}</p>
		{:else if detail}
			<div class="border-border flex items-center gap-3 border-b px-4 py-4">
				<span
					class="ring-line-strong bg-raised grid size-10 shrink-0 place-items-center rounded-lg text-base font-medium ring-1 ring-inset"
					aria-hidden="true">{accountInitial(detail.email)}</span
				>
				<div class="min-w-0">
					<h2 class="truncate text-[13px] font-semibold">{detail.email}</h2>
					<p class="text-dim text-[11.5px]">
						{t('app.admin.created')}: {formatDateTime(detail.created_at)}{detail.mail_verified
							? ` · ${t('app.admin.verified')}`
							: ''}
					</p>
				</div>
			</div>
			<dl class="border-border grid grid-cols-2 border-b">
				<div class="border-border border-e px-4 py-3">
					<dt class="text-dim text-[11px]">{t('app.admin.generations')}</dt>
					<dd class="mt-0.5 text-sm tabular-nums">{detail.generations ?? 0}</dd>
				</div>
				<div class="px-4 py-3">
					<dt class="text-dim text-[11px]">{t('app.admin.assets')}</dt>
					<dd class="mt-0.5 text-sm tabular-nums">{detail.assets ?? 0}</dd>
				</div>
			</dl>

			<div class="border-border flex flex-col gap-4 border-b px-4 py-4">
				<div>
					<p id="admin-role-label" class="mb-2 text-xs font-semibold">{t('app.admin.role')}</p>
					<div
						class="bg-field border-line-strong flex gap-0.5 rounded-[7px] border p-0.5"
						role="group"
						aria-labelledby="admin-role-label"
					>
						{#each ['viewer', 'user', 'admin'] as const as role (role)}
							<button
								type="button"
								aria-pressed={selectedRole === role}
								class={'h-[30px] flex-1 rounded-[5px] text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
									(selectedRole === role
										? 'bg-hover text-foreground ring-line-strong ring-1 ring-inset'
										: 'text-muted-foreground hover:text-foreground')}
								onclick={() => (selectedRole = role)}>{t(roleLabelKey(role))}</button
							>
						{/each}
					</div>
					{#if selectedRole !== detail.role}
						<Button
							variant="outline"
							size="sm"
							class="mt-2"
							disabled={actionSubmitting}
							onclick={requestRoleChange}
						>
							{t('app.admin.change_role_action')}
						</Button>
					{/if}
				</div>
				<div>
					{#if detail.state === 'purging'}
						<p class="mb-2 text-xs font-semibold">{t('app.admin.change_state')}</p>
						<span class="tag tag-danger">{t('app.admin.state_purging')}</span>
					{:else}
						<label for="admin-state" class="mb-2 block text-xs font-semibold"
							>{t('app.admin.state')}</label
						>
						<div class="flex gap-2">
							<select
								id="admin-state"
								class="bg-field border-line-strong focus-visible:ring-ring/60 h-8 min-w-0 flex-1 rounded-md border px-2 text-[13px] outline-none focus-visible:ring-2"
								bind:value={selectedState}
							>
								<option value="active">{t('app.admin.state_active')}</option>
								<option value="suspended">{t('app.admin.state_suspended')}</option>
								<option value="disabled">{t('app.admin.state_disabled')}</option>
								<option value="deletion_pending">{t('app.admin.state_deletion_pending')}</option>
							</select>
							<Button
								variant="outline"
								size="sm"
								disabled={selectedState === detail.state || actionSubmitting}
								onclick={requestStateChange}
							>
								{t('app.admin.change_state_action')}
							</Button>
						</div>
					{/if}
					{#if detail.state === 'deletion_pending'}
						<Button
							variant="outline"
							size="sm"
							class="mt-2"
							disabled={actionSubmitting}
							onclick={() => detail && requestAction(detail, { kind: 'restore' })}
						>
							{t('app.admin.restore')}
						</Button>
					{/if}
					<p class="text-dim mt-2 text-[11.5px]">{t('app.admin.changes_audited')}</p>
				</div>
				{#if detailError}
					<p role="alert" class="text-destructive text-[13px]">{detailError}</p>
				{/if}
			</div>

			<section class="flex flex-col gap-2.5 px-4 py-4">
				<div class="flex items-center justify-between gap-3">
					<h2 class="text-xs font-semibold">{t('app.admin.recent_generations')}</h2>
					<span data-testid="admin-generation-count" class="text-dim text-xs tabular-nums">
						{generations.length}
					</span>
				</div>
				{#if generations.length === 0}
					<p class="text-muted-foreground text-[13px]">{t('app.admin.generations_empty')}</p>
				{:else}
					<div class="grid grid-cols-4 gap-1.5">
						{#each generations as generation (generation.id)}
							{#if generation.assets.length > 0}
								<img
									src={generation.assets[0].thumbnail_url ?? generation.assets[0].url}
									alt={generation.params.prompt ?? t('app.admin.untitled_generation')}
									title={generation.params.prompt ?? generation.model_id}
									class="ring-line-strong aspect-square w-full rounded-md object-cover ring-1"
									width="80"
									height="80"
									loading="lazy"
								/>
							{/if}
						{/each}
					</div>
				{/if}
			</section>
		{:else}
			<p class="text-muted-foreground p-5 text-[13px]">{t('app.admin.select_user')}</p>
		{/if}
	</aside>
</div>

<Dialog.Root bind:open={confirmationOpen}>
	<Dialog.Portal>
		<Dialog.Overlay class="fixed inset-0 z-50 bg-black/60" />
		<Dialog.Content
			class="bg-popover text-popover-foreground border-line-strong fixed top-1/2 left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-xl border p-6 shadow-[0_12px_40px_rgba(0,0,0,0.45)]"
		>
			<Dialog.Title class="text-lg font-semibold">{t('app.admin.confirmation_title')}</Dialog.Title>
			<Dialog.Description class="text-muted-foreground mt-2 text-sm">
				{confirmationMessage}
			</Dialog.Description>
			{#if confirmationNeedsAttestation}
				<label class="mt-4 flex items-start gap-2 text-sm">
					<input
						data-testid="admin-attest-identity"
						type="checkbox"
						aria-required="true"
						class="border-input mt-0.5 size-4 shrink-0 accent-primary"
						bind:checked={attestIdentity}
					/>
					<span>{t('app.admin.attest_identity')}</span>
				</label>
			{/if}
			{#if actionError}
				<p role="alert" class="text-destructive mt-3 text-sm">{actionError}</p>
			{/if}
			<div class="mt-6 flex justify-end gap-2">
				<Button variant="outline" disabled={actionSubmitting} onclick={cancelConfirmation}>
					{t('app.admin.cancel')}
				</Button>
				<Button
					disabled={actionSubmitting || (confirmationNeedsAttestation && !attestIdentity)}
					onclick={confirmAction}
				>
					{actionSubmitting ? t('app.admin.saving') : t('app.admin.confirm')}
				</Button>
			</div>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
