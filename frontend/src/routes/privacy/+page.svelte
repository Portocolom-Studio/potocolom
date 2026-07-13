<script lang="ts">
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
</script>

<svelte:head>
	<title>potocolom - {t('privacy.title')}</title>
	<meta name="robots" content="noindex" />
</svelte:head>

<header>
	<a class="logo" href={resolve('/')}>potocolom<span>_</span></a>
	<LanguageToggle />
</header>

<article>
	<h1>{t('privacy.title')}</h1>
	<p class="intro">{t('privacy.intro')}</p>
	<h2>{t('privacy.waitlist_h')}</h2>
	<p>{t('privacy.waitlist_body')}</p>
	<h2>{t('privacy.cookies_h')}</h2>
	<p>{t('privacy.cookies_body')}</p>
	<h2>{t('privacy.processors_h')}</h2>
	<p>{t('privacy.processors_body')}</p>
	<h2>{t('privacy.rights_h')}</h2>
	<p>{t('privacy.rights_body')}</p>
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

	.intro {
		font-size: 1.05rem;
		color: var(--text);
	}

	h2 {
		font-size: 1.15rem;
		margin-top: 2rem;
	}

	p {
		color: var(--text-muted);
	}
</style>
