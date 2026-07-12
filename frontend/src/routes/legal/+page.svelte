<script lang="ts">
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
</script>

<svelte:head>
	<title>potocolom - {t('legal.title')}</title>
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
	<h1 class="text-3xl font-semibold tracking-tight">{t('legal.title')}</h1>
	<h2 class="mt-8 text-lg font-medium">{t('legal.operator_h')}</h2>
	<p class="text-muted-foreground mt-2">{t('legal.operator_body')}</p>
	<h2 class="mt-8 text-lg font-medium">{t('legal.purpose_h')}</h2>
	<p class="text-muted-foreground mt-2">{t('legal.purpose_body')}</p>
	<h2 class="mt-8 text-lg font-medium">{t('legal.law_h')}</h2>
	<p class="text-muted-foreground mt-2">{t('legal.law_body')}</p>
</article>
