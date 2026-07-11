<script lang="ts">
	// $env/static/public inlines the value at build time; the SPA fallback shell
	// never loads env.js, so the dynamic variant is always empty in this setup
	import { PUBLIC_WAITLIST_URL } from '$env/static/public';
	import { getLocale, t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';

	const endpoint = PUBLIC_WAITLIST_URL;

	let email = $state('');
	let honeypot = $state('');
	let status = $state<'idle' | 'sending' | 'done' | 'already' | 'error'>('idle');

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (honeypot !== '' || status === 'sending') return;
		status = 'sending';
		try {
			const response = await fetch(endpoint, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ email, locale: getLocale() })
			});
			if (!response.ok) throw new Error(String(response.status));
			const result = await response.json();
			status = result.status === 'exists' ? 'already' : 'done';
		} catch {
			status = 'error';
		}
	}
</script>

{#if endpoint}
	<section id="waitlist">
		<p class="kicker">{t('wl.kicker')}</p>
		<h2>{t('wl.title')}</h2>
		<p class="sub">{t('wl.sub')}</p>
		{#if status === 'done' || status === 'already'}
			<p class="result" role="status">
				{status === 'done' ? t('wl.done') : t('wl.already')}
			</p>
		{:else}
			<form onsubmit={submit}>
				<input
					type="email"
					name="email"
					required
					placeholder={t('wl.placeholder')}
					bind:value={email}
					disabled={status === 'sending'}
				/>
				<input
					type="text"
					name="website"
					class="hp"
					tabindex="-1"
					autocomplete="off"
					aria-hidden="true"
					bind:value={honeypot}
				/>
				<button type="submit" disabled={status === 'sending'}>
					{status === 'sending' ? t('wl.sending') : t('wl.cta')}
				</button>
			</form>
			{#if status === 'error'}
				<p class="error" role="alert">{t('wl.error')}</p>
			{/if}
		{/if}
		<p class="note"><a href={resolve('/privacy')}>{t('wl.privacy_note')}</a></p>
	</section>
{/if}

<style>
	section {
		max-width: var(--content-width);
		margin: 0 auto;
		padding: 5.5rem clamp(1rem, 4vw, 3rem) 1rem;
		text-align: center;
	}

	.kicker {
		color: var(--accent-2);
		font-size: 0.85rem;
		font-weight: 500;
		letter-spacing: 0.22em;
		text-transform: uppercase;
		margin-bottom: 0.4rem;
	}

	h2 {
		font-size: clamp(1.7rem, 3.5vw, 2.4rem);
	}

	.sub {
		color: var(--text-muted);
		max-width: 520px;
		margin: 0 auto 1.8rem;
	}

	form {
		display: flex;
		gap: 0.7rem;
		justify-content: center;
		flex-wrap: wrap;
	}

	input[type='email'] {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 0.75rem 1.3rem;
		color: var(--text);
		font: inherit;
		width: min(22rem, 100%);
	}

	input[type='email']:focus {
		outline: 2px solid var(--accent-2);
		outline-offset: 1px;
		border-color: transparent;
	}

	.hp {
		position: absolute;
		left: -9999px;
		width: 1px;
		height: 1px;
		opacity: 0;
	}

	button {
		border: none;
		border-radius: 999px;
		padding: 0.75rem 1.6rem;
		font-weight: 600;
		font-size: 0.95rem;
		cursor: pointer;
		background: var(--gradient-accent);
		color: #06080f;
		box-shadow: 0 0 24px rgba(124, 111, 255, 0.35);
	}

	button:disabled {
		opacity: 0.7;
		cursor: wait;
	}

	.result {
		font-size: 1.1rem;
		color: var(--accent-2);
	}

	.error {
		color: #f87171;
		margin-top: 0.8rem;
	}

	.note {
		margin-top: 1.2rem;
		font-size: 0.8rem;
	}

	.note a {
		color: var(--text-muted);
	}
</style>
