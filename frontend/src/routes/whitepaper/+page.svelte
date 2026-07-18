<script lang="ts">
	import SiteLandingHeader from '$lib/components/SiteLandingHeader.svelte';
	import ScrollToTop from '$lib/components/ScrollToTop.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
	import { Button } from '$lib/components/ui/button';
	import { Separator } from '$lib/components/ui/separator';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';

	const sections = [
		{ id: 's1', title: 'wp.s1_title', paragraphs: ['wp.s1_p1', 'wp.s1_p2'] },
		{
			id: 's2',
			title: 'wp.s2_title',
			paragraphs: ['wp.s2_p1', 'wp.s2_p2', 'wp.s2_p3'],
			figure: { src: '/whitepaper/under-the-hood.png', cap: 'wp.fig_arch_cap' }
		},
		{
			id: 's3',
			title: 'wp.s3_title',
			paragraphs: ['wp.s3_p1', 'wp.s3_p2', 'wp.s3_p3'],
			figure: { src: '/whitepaper/realtime-loop.png', cap: 'wp.fig_loop_cap' }
		},
		{ id: 's4', title: 'wp.s4_title', paragraphs: ['wp.s4_p1', 'wp.s4_p2', 'wp.s4_p3'] },
		{ id: 's5', title: 'wp.s5_title', paragraphs: ['wp.s5_p1', 'wp.s5_p2', 'wp.s5_p3'] },
		{
			id: 's6',
			title: 'wp.s6_title',
			paragraphs: ['wp.s6_p1', 'wp.s6_p2', 'wp.s6_p3', 'wp.s6_p4']
		},
		{
			id: 's7',
			title: 'wp.s7_title',
			paragraphs: ['wp.s7_p1', 'wp.s7_p2', 'wp.s7_p3'],
			figure: { src: '/whitepaper/credit-lifecycle.png', cap: 'wp.fig_credits_cap' }
		},
		{
			id: 's8',
			title: 'wp.s8_title',
			paragraphs: ['wp.s8_p1', 'wp.s8_p2'],
			figure: { src: '/whitepaper/failure-map.png', cap: 'wp.fig_failures_cap' }
		},
		{ id: 's9', title: 'wp.s9_title', paragraphs: ['wp.s9_p1', 'wp.s9_p2'] }
	] as const;
</script>

<svelte:head>
	<title>potocolom - {t('wp.title')}</title>
	<meta name="description" content={t('wp.sub')} />
</svelte:head>

<SiteLandingHeader current="whitepaper" />

<div class="mx-auto max-w-6xl px-4 pt-24 sm:px-6 sm:pt-28">
	<h1 class="max-w-3xl text-4xl font-bold tracking-tight sm:text-5xl">{t('wp.title')}</h1>
	<p class="text-muted-foreground mt-4 max-w-2xl text-lg">{t('wp.sub')}</p>
</div>

<div class="mx-auto grid max-w-6xl gap-12 px-4 py-12 sm:px-6 lg:grid-cols-[220px_1fr]">
	<aside class="hidden self-start lg:sticky lg:top-20 lg:block" aria-label={t('wp.toc')}>
		<p class="text-muted-foreground text-xs font-semibold tracking-[0.18em] uppercase">
			{t('wp.toc')}
		</p>
		<ol class="mt-3 flex flex-col border-l">
			{#each sections as section (section.id)}
				<li>
					<a
						class="text-muted-foreground hover:text-foreground hover:border-primary -ml-px block border-l-2 border-transparent py-1.5 pl-4 text-sm transition-colors"
						href="#{section.id}"
					>
						{t(section.title)}
					</a>
				</li>
			{/each}
		</ol>
	</aside>

	<article>
		{#each sections as section, index (section.id)}
			<section
				id={section.id}
				class={index === sections.length - 1 ? 'scroll-mt-20' : 'mb-14 scroll-mt-20'}
			>
				<h2 class="text-2xl font-semibold">{t(section.title)}</h2>
				{#each section.paragraphs as paragraph (paragraph)}
					<p class="text-muted-foreground mt-3 max-w-[68ch] text-base leading-relaxed">
						{t(paragraph)}
					</p>
				{/each}
				{#if 'figure' in section}
					<figure class="mt-6 rounded-xl border bg-white p-3">
						<img
							class="w-full rounded-lg"
							src={section.figure.src}
							alt={t(section.figure.cap)}
							loading="lazy"
						/>
						<figcaption class="pt-2 pl-1 text-xs text-slate-600">
							{t(section.figure.cap)}
						</figcaption>
					</figure>
				{/if}
			</section>
		{/each}
		<div class="mt-6 flex flex-wrap gap-3">
			<Button variant="outline" href="{repoUrl}/tree/main/docs">
				{t('wp.cta_docs')}
			</Button>
			<Button variant="outline" href={resolve('/benchmark')}>
				{t('wp.cta_benchmark')}
			</Button>
		</div>
	</article>
</div>

<Separator />

<footer
	class="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-4 py-8 sm:px-6"
>
	<p class="text-muted-foreground text-sm">{t('footer.tagline')}</p>
	<nav class="text-muted-foreground flex flex-wrap gap-5 text-sm">
		<a class="hover:text-foreground transition-colors" href={repoUrl}>{t('footer.github')}</a>
		<a class="hover:text-foreground transition-colors" href="{repoUrl}/tree/main/docs">
			{t('footer.docs')}
		</a>
		<a class="hover:text-foreground transition-colors" href={resolve('/legal')}>
			{t('footer.legal')}
		</a>
		<a class="hover:text-foreground transition-colors" href={resolve('/privacy')}>
			{t('footer.privacy')}
		</a>
		<a class="hover:text-foreground transition-colors" href="mailto:admin@leonfuller.com">
			{t('footer.contact')}
		</a>
	</nav>
</footer>

<ScrollToTop />
