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
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
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

	async function apiError(response: Response): Promise<string> {
		if (response.status === 403) void recheckAdminAccess();
		const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
		return adminErrorMessage(body?.detail, response.statusText);
	}

	function displayDate(value: string | null): string {
		if (!value) return '-';
		const date = Date.parse(value);
		return Number.isFinite(date) ? new Date(date).toLocaleString() : value;
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

<div class="grid min-h-0 gap-4 xl:grid-cols-[minmax(18rem,0.85fr)_minmax(0,1.65fr)]">
	<Card.Root class="min-h-0 overflow-hidden p-0 [--card-spacing:0]">
		<Card.Header class="border-border border-b px-4 py-3">
			<div class="flex items-center justify-between gap-3">
				<div>
					<Card.Title class="text-base">{t('app.admin.users_title')}</Card.Title>
					<Card.Description>{t('app.admin.users_sub')}</Card.Description>
				</div>
				<span data-testid="admin-user-count" class="text-muted-foreground text-xs tabular-nums">
					{t('app.admin.users_count').replace('{count}', String(users.length))}
				</span>
			</div>
			<Input
				type="search"
				data-testid="admin-user-search"
				class="mt-3"
				aria-label={t('app.admin.search_users')}
				placeholder={t('app.admin.search_users')}
				bind:value={searchQuery}
			/>
		</Card.Header>
		<Card.Content class="max-h-[55svh] overflow-auto p-0 xl:max-h-none xl:h-full">
			{#if usersLoading}
				<p class="text-muted-foreground p-4 text-sm">{t('app.admin.loading_users')}</p>
			{:else if usersError}
				<p role="alert" class="text-destructive p-4 text-sm">{usersError}</p>
			{:else if users.length === 0}
				<p class="text-muted-foreground p-4 text-sm">{t('app.admin.users_empty')}</p>
			{:else}
				<div class="divide-border divide-y">
					{#each users as user (user.id)}
						<button
							type="button"
							data-testid="admin-user-row"
							class="hover:bg-muted/60 flex w-full flex-col items-start gap-1 px-4 py-3 text-left transition-colors"
							class:bg-muted={selectedId === user.id}
							aria-pressed={selectedId === user.id}
							onclick={() => selectUser(user.id)}
						>
							<span class="truncate text-sm font-medium">{user.email}</span>
							<span class="text-muted-foreground flex flex-wrap gap-x-2 text-xs">
								<span>{t(roleLabelKey(user.role))}</span>
								<span>{t(stateLabelKey(user.state))}</span>
							</span>
						</button>
					{/each}
				</div>
				{#if nextCursor !== null}
					<div class="p-3">
						<Button
							variant="outline"
							size="sm"
							data-testid="admin-users-load-more"
							disabled={loadingMore}
							onclick={loadMoreUsers}
						>
							{loadingMore ? t('app.admin.loading_users') : t('app.admin.load_more')}
						</Button>
						{#if loadMoreError}
							<p role="alert" class="text-destructive mt-2 text-sm">{loadMoreError}</p>
						{/if}
					</div>
				{/if}
			{/if}
		</Card.Content>
	</Card.Root>

	<Card.Root class="min-h-0 overflow-auto p-0 [--card-spacing:0]">
		{#if detailLoading && !detail}
			<p class="text-muted-foreground p-5 text-sm">{t('app.admin.loading_user')}</p>
		{:else if detailError && !detail}
			<p role="alert" class="text-destructive p-5 text-sm">{detailError}</p>
		{:else if detail}
			<Card.Header class="border-border border-b px-5 py-4">
				<Card.Title class="break-all text-lg">{detail.email}</Card.Title>
				<Card.Description>
					{t('app.admin.created')}: {displayDate(detail.created_at)}
				</Card.Description>
			</Card.Header>
			<Card.Content class="flex flex-col gap-5 p-5">
				<div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
					<div class="bg-muted/50 rounded-lg p-3">
						<p class="text-muted-foreground text-xs">{t('app.admin.role')}</p>
						<p class="mt-1 text-sm font-medium">{t(roleLabelKey(detail.role))}</p>
					</div>
					<div class="bg-muted/50 rounded-lg p-3">
						<p class="text-muted-foreground text-xs">{t('app.admin.state')}</p>
						<p class="mt-1 text-sm font-medium">{t(stateLabelKey(detail.state))}</p>
					</div>
					<div class="bg-muted/50 rounded-lg p-3">
						<p class="text-muted-foreground text-xs">{t('app.admin.generations')}</p>
						<p class="mt-1 text-sm font-medium tabular-nums">{detail.generations ?? 0}</p>
					</div>
					<div class="bg-muted/50 rounded-lg p-3">
						<p class="text-muted-foreground text-xs">{t('app.admin.assets')}</p>
						<p class="mt-1 text-sm font-medium tabular-nums">{detail.assets ?? 0}</p>
					</div>
				</div>

				<div class="flex flex-wrap items-end gap-3">
					<label class="flex min-w-40 flex-col gap-1 text-xs font-medium">
						{t('app.admin.change_role')}
						<select
							class="border-input bg-background h-9 rounded-md border px-2 text-sm"
							bind:value={selectedRole}
						>
							<option value="admin">{t('app.admin.role_admin')}</option>
							<option value="user">{t('app.admin.role_user')}</option>
							<option value="viewer">{t('app.admin.role_viewer')}</option>
						</select>
					</label>
					<Button
						variant="outline"
						disabled={selectedRole === detail.role || actionSubmitting}
						onclick={requestRoleChange}
					>
						{t('app.admin.change_role_action')}
					</Button>
					{#if detail.state === 'purging'}
						<div class="flex min-w-44 flex-col gap-1 text-xs font-medium">
							<span>{t('app.admin.change_state')}</span>
							<span class="text-muted-foreground h-9 content-center text-sm font-normal">
								{t('app.admin.state_purging')}
							</span>
						</div>
					{:else}
						<label class="flex min-w-44 flex-col gap-1 text-xs font-medium">
							{t('app.admin.change_state')}
							<select
								class="border-input bg-background h-9 rounded-md border px-2 text-sm"
								bind:value={selectedState}
							>
								<option value="active">{t('app.admin.state_active')}</option>
								<option value="suspended">{t('app.admin.state_suspended')}</option>
								<option value="disabled">{t('app.admin.state_disabled')}</option>
								<option value="deletion_pending">{t('app.admin.state_deletion_pending')}</option>
							</select>
						</label>
						<Button
							variant="outline"
							disabled={selectedState === detail.state || actionSubmitting}
							onclick={requestStateChange}
						>
							{t('app.admin.change_state_action')}
						</Button>
					{/if}
					{#if detail.state === 'deletion_pending'}
						<Button
							variant="outline"
							class="border-primary/40"
							disabled={actionSubmitting}
							onclick={() => detail && requestAction(detail, { kind: 'restore' })}
						>
							{t('app.admin.restore')}
						</Button>
					{/if}
				</div>

				{#if detailError}
					<p role="alert" class="text-destructive text-sm">{detailError}</p>
				{/if}

				<section class="flex flex-col gap-2">
					<div class="flex items-center justify-between gap-3">
						<h2 class="text-sm font-semibold">{t('app.admin.recent_generations')}</h2>
						<span
							data-testid="admin-generation-count"
							class="text-muted-foreground text-xs tabular-nums"
						>
							{generations.length}
						</span>
					</div>
					{#if generations.length === 0}
						<p class="text-muted-foreground text-sm">{t('app.admin.generations_empty')}</p>
					{:else}
						<div class="no-scrollbar flex gap-2 overflow-x-auto pb-1">
							{#each generations as generation (generation.id)}
								{#if generation.assets.length > 0}
									<img
										src={generation.assets[0].thumbnail_url ?? generation.assets[0].url}
										alt={generation.params.prompt ?? t('app.admin.untitled_generation')}
										title={generation.params.prompt ?? generation.model_id}
										class="border-border h-24 w-24 shrink-0 rounded-lg border object-cover"
										loading="lazy"
									/>
								{/if}
							{/each}
						</div>
					{/if}
				</section>
			</Card.Content>
		{:else}
			<Card.Content class="p-5">
				<p class="text-muted-foreground text-sm">{t('app.admin.select_user')}</p>
			</Card.Content>
		{/if}
	</Card.Root>
</div>

<Dialog.Root bind:open={confirmationOpen}>
	<Dialog.Portal>
		<Dialog.Overlay class="bg-background/80 fixed inset-0 z-50 backdrop-blur-sm" />
		<Dialog.Content
			class="bg-popover text-popover-foreground fixed top-1/2 left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border p-6 shadow-lg"
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
