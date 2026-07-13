<script lang="ts">
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { Button } from '$lib/components/ui/button';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';

	let { current }: { current?: 'benchmark' | 'whitepaper' } = $props();

	const home = resolve('/');

	const navClass = (page?: 'benchmark' | 'whitepaper') =>
		'hover:text-foreground transition-colors' +
		(current === page ? ' text-foreground font-medium' : '');

	$effect(() => {
		const html = document.documentElement;
		const body = document.body;
		html.classList.add('no-scrollbar');
		body.classList.add('no-scrollbar', 'overflow-x-clip');
		return () => {
			html.classList.remove('no-scrollbar');
			body.classList.remove('no-scrollbar', 'overflow-x-clip');
		};
	});
</script>

<header class="bg-background/70 fixed inset-x-0 top-0 z-50 border-b backdrop-blur-md">
	<div class="mx-auto flex h-16 max-w-6xl items-center gap-6 px-4 sm:px-6">
		<a class="text-base font-bold" href={home}>
			potocolom<span class="text-primary">_</span>
		</a>
		<nav class="text-muted-foreground hidden gap-6 text-base md:flex">
			<a class="hover:text-foreground transition-colors" href="{home}#features">{t('nav.features')}</a>
			<a class="hover:text-foreground transition-colors" href="{home}#pricing">{t('nav.pricing')}</a>
			<a class="hover:text-foreground transition-colors" href="{home}#open">{t('nav.open')}</a>
			<a class={navClass('whitepaper')} href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
			<a class={navClass('benchmark')} href={resolve('/benchmark')}>{t('nav.benchmark')}</a>
		</nav>
		<div class="ml-auto flex items-center gap-3">
			<LanguageToggle />
			<Button size="sm" variant="gradient" href={resolve('/app')}>{t('nav.launch')}</Button>
		</div>
	</div>
</header>
