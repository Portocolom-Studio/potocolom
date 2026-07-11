<script lang="ts">
	import { page } from '$app/state';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
</script>

<svelte:head>
	<title>potocolom - {page.status === 404 ? t('error.title404') : t('error.titleOther')}</title>
</svelte:head>

<main>
	<p class="code">{page.status}</p>
	<h1>{page.status === 404 ? t('error.title404') : t('error.titleOther')}</h1>
	<p class="message">{t('error.message')}</p>
	<a class="button" href={resolve('/')}>{t('error.home')}</a>
</main>

<style>
	main {
		min-height: 100vh;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		text-align: center;
		padding: 2rem;
		gap: 0.6rem;
	}

	.code {
		font-size: 5rem;
		font-weight: 700;
		letter-spacing: -0.03em;
		background: var(--gradient-accent);
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
		margin: 0;
	}

	h1 {
		font-size: 1.6rem;
		margin: 0;
	}

	.message {
		color: var(--text-muted);
		max-width: 46ch;
	}

	.button {
		margin-top: 1.4rem;
		display: inline-block;
		padding: 0.7rem 1.5rem;
		border-radius: 999px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		text-decoration: none;
		font-weight: 600;
	}

	.button:hover {
		background: var(--surface-hover);
	}
</style>
