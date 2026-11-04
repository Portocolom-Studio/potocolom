<script lang="ts">
	import PasswordInput from '$lib/components/password-input.svelte';
	import { resolve } from '$app/paths';
	import { apiFetch } from '$lib/api';
	import { refusalKey } from '$lib/account-logic';
	import { createSubmitGuard, parseAuthError } from '$lib/auth-flow';
	import { t, passwordLengthMessage } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';

	// Shown only while the install runs without accounts. The one caller is the
	// implicit administrator, so the server takes that row over the way setup does.
	const guard = createSubmitGuard();
	const cardClass = 'bg-card border-border rounded-[10px] border p-0 ring-0 [--card-spacing:0]';

	let email = $state('');
	let password = $state('');
	let confirmed = $state(false);
	let submitting = $state(false);
	let error = $state('');

	async function refusalText(response: Response): Promise<string> {
		if (response.status === 409) return t('app.account.turn_on_refused');
		const parsed = await parseAuthError(response);
		const key = refusalKey(parsed.message);
		return key === null ? t('app.account.request_failed') : t(key);
	}

	async function turnOn(event: SubmitEvent) {
		event.preventDefault();
		await guard.run(async () => {
			error = passwordLengthMessage(password);
			if (error) return;
			submitting = true;
			try {
				const response = await apiFetch('/api/v1/auth/enable', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ email, password })
				});
				if (response.status === 204) {
					// The server has set this browser's session, but the studio reads
					// its account once, at load. A full load re-reads it, where a client
					// navigation would keep the no-accounts state. replace keeps Back
					// from returning to this form.
					location.replace(resolve('/app'));
					return;
				}
				error = await refusalText(response);
			} catch {
				error = t('app.account.request_failed');
			} finally {
				submitting = false;
			}
		});
	}
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="no-scrollbar min-h-0 flex-1 overflow-y-auto">
		<div class="flex w-full max-w-[760px] flex-col gap-4 px-4 py-8 md:px-12">
			<div>
				<h1 class="text-xl font-semibold tracking-[-0.02em]">{t('app.account.turn_on_title')}</h1>
				<p class="text-muted-foreground mt-1 text-[12.5px] leading-relaxed">
					{t('app.account.turn_on_sub')}
				</p>
			</div>
			<Card.Root class={cardClass}>
				<Card.Content class="flex flex-col gap-4 p-4">
					<p class="text-muted-foreground text-sm">{t('app.account.turn_on_warning')}</p>
					<form class="flex flex-col gap-4" onsubmit={turnOn}>
						<Field.Field>
							<Label for="turn-on-email">{t('auth.login.email_label')}</Label>
							<Input
								id="turn-on-email"
								name="email"
								type="email"
								autocomplete="username"
								spellcheck={false}
								required
								bind:value={email}
							/>
						</Field.Field>
						<Field.Field>
							<Label for="turn-on-password">{t('auth.join.password_label')}</Label>
							<PasswordInput
								id="turn-on-password"
								name="password"
								autocomplete="new-password"
								required
								bind:value={password}
							/>
						</Field.Field>
						<label class="flex items-center gap-2 text-sm">
							<input id="turn-on-confirm" type="checkbox" bind:checked={confirmed} />
							{t('app.account.turn_on_confirm')}
						</label>
						{#if error}
							<p class="text-destructive text-sm" role="alert">{error}</p>
						{/if}
						<Button type="submit" disabled={submitting || !confirmed}>
							{submitting ? t('auth.submitting') : t('app.account.turn_on_submit')}
						</Button>
					</form>
				</Card.Content>
			</Card.Root>
		</div>
	</div>
</div>
