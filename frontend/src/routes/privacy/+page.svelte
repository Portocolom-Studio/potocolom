<script lang="ts">
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
</script>

<svelte:head>
	<title>potocolom - {t('privacy.title')}</title>
	<meta name="robots" content="noindex" />
</svelte:head>

<header class="border-b">
	<div class="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-6">
		<a class="text-base font-bold tracking-tight" href={resolve('/')}>
			potocolom<span class="text-primary">_</span>
		</a>
		<LanguageToggle />
	</div>
</header>

<article class="mx-auto max-w-[68ch] px-4 py-12 sm:px-6">
	<h1 class="text-3xl font-semibold tracking-tight">{t('privacy.title')}</h1>
	<p class="mt-4 text-base">{t('privacy.intro')}</p>
	<h2 class="mt-8 text-lg font-medium">{t('privacy.waitlist_h')}</h2>
	<p class="text-muted-foreground mt-2">{t('privacy.waitlist_body')}</p>
	<h2 class="mt-8 text-lg font-medium">{t('privacy.cookies_h')}</h2>
	<p class="text-muted-foreground mt-2">{t('privacy.cookies_body')}</p>
	<h2 class="mt-8 text-lg font-medium">{t('privacy.processors_h')}</h2>
	<p class="text-muted-foreground mt-2">{t('privacy.processors_body')}</p>
	<h2 class="mt-8 text-lg font-medium">{t('privacy.rights_h')}</h2>
	<p class="text-muted-foreground mt-2">{t('privacy.rights_body')}</p>
</article>
