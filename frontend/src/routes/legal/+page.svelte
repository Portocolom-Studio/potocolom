<script lang="ts">
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
</script>

<svelte:head>
	<title>potocolom - {t('legal.title')}</title>
	<meta name="robots" content="noindex" />
</svelte:head>

<header>
	<a class="logo" href={resolve('/')}>potocolom<span>_</span></a>
	<LanguageToggle />
</header>

<article>
	<h1>{t('legal.title')}</h1>
	<h2>{t('legal.operator_h')}</h2>
	<p>{t('legal.operator_body')}</p>
	<h2>{t('legal.purpose_h')}</h2>
	<p>{t('legal.purpose_body')}</p>
	<h2>{t('legal.law_h')}</h2>
	<p>{t('legal.law_body')}</p>
</article>

<style>
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.9rem clamp(1rem, 4vw, 3rem);
		border-bottom: 1px solid var(--border);
	}

	.logo {
		font-weight: 700;
		text-decoration: none;
	}

	.logo span {
		color: var(--accent-2);
	}

	article {
		max-width: 68ch;
		margin: 0 auto;
		padding: 3rem clamp(1rem, 4vw, 3rem) 5rem;
	}

	h1 {
		font-size: 2rem;
	}

	h2 {
		font-size: 1.15rem;
		margin-top: 2rem;
	}

	p {
		color: var(--text-muted);
	}
</style>
