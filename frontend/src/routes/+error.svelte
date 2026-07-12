<script lang="ts">
	import { page } from '$app/state';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
	import { Button } from '$lib/components/ui/button';
</script>

<svelte:head>
	<title>potocolom - {page.status === 404 ? t('error.title404') : t('error.titleOther')}</title>
</svelte:head>

<main class="grid min-h-screen place-items-center px-6 text-center">
	<div class="flex flex-col items-center gap-3">
		<p class="text-primary text-7xl font-bold tracking-tight">{page.status}</p>
		<h1 class="text-2xl font-semibold tracking-tight">
			{page.status === 404 ? t('error.title404') : t('error.titleOther')}
		</h1>
		<p class="text-muted-foreground max-w-md">{t('error.message')}</p>
		<Button class="mt-4" variant="outline" href={resolve('/')}>{t('error.home')}</Button>
	</div>
</main>
