<script lang="ts">
	import SiteLandingHeader from '$lib/components/SiteLandingHeader.svelte';
	import ScrollToTop from '$lib/components/ScrollToTop.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
	import { Button } from '$lib/components/ui/button';
	import { Separator } from '$lib/components/ui/separator';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';

	const sections = [
		{ id: 's1', title: 'wp.s1_title' },
		{ id: 's2', title: 'wp.s2_title' },
		{ id: 's3', title: 'wp.s3_title' },
		{ id: 's4', title: 'wp.s4_title' },
		{ id: 's5', title: 'wp.s5_title' },
		{ id: 's6', title: 'wp.s6_title' },
		{ id: 's7', title: 'wp.s7_title' }
	] as const;
</script>

<svelte:head>
	<title>potocolom - {t('wp.title')}</title>
	<meta name="description" content={t('wp.sub')} />
</svelte:head>

<SiteLandingHeader current="whitepaper" />

<div class="mx-auto max-w-6xl px-4 pt-16 sm:px-6">
	<p class="text-primary text-xs font-semibold tracking-[0.22em] uppercase">{t('wp.kicker')}</p>
	<h1 class="mt-2 max-w-3xl text-4xl font-bold tracking-tight sm:text-5xl">{t('wp.title')}</h1>
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
		<section id="s1" class="mb-14 scroll-mt-20">
			<h2 class="text-2xl font-semibold">{t('wp.s1_title')}</h2>
			<p class="text-muted-foreground mt-3 max-w-[68ch] text-base leading-relaxed">
				{t('wp.s1_body')}
			</p>
			<figure class="mt-6 rounded-xl border bg-white p-3">
				<img
					class="w-full rounded-lg"
					src="/whitepaper/platform-overview.png"
					alt={t('wp.fig1_cap')}
					loading="lazy"
				/>
				<figcaption class="pt-2 pl-1 text-xs text-slate-600">{t('wp.fig1_cap')}</figcaption>
			</figure>
		</section>

		<section id="s2" class="mb-14 scroll-mt-20">
			<h2 class="text-2xl font-semibold">{t('wp.s2_title')}</h2>
			<p class="text-muted-foreground mt-3 max-w-[68ch] text-base leading-relaxed">
				{t('wp.s2_body')}
			</p>
			<figure class="mt-6 rounded-xl border bg-white p-3">
				<img
					class="w-full rounded-lg"
					src="/whitepaper/realtime-loop.png"
					alt={t('wp.fig2_cap')}
					loading="lazy"
				/>
				<figcaption class="pt-2 pl-1 text-xs text-slate-600">{t('wp.fig2_cap')}</figcaption>
			</figure>
		</section>

		<section id="s3" class="mb-14 scroll-mt-20">
			<h2 class="text-2xl font-semibold">{t('wp.s3_title')}</h2>
			<p class="text-muted-foreground mt-3 max-w-[68ch] text-base leading-relaxed">
				{t('wp.s3_body')}
			</p>
		</section>

		<section id="s4" class="mb-14 scroll-mt-20">
			<h2 class="text-2xl font-semibold">{t('wp.s4_title')}</h2>
			<p class="text-muted-foreground mt-3 max-w-[68ch] text-base leading-relaxed">
				{t('wp.s4_body')}
			</p>
		</section>

		<section id="s5" class="mb-14 scroll-mt-20">
			<h2 class="text-2xl font-semibold">{t('wp.s5_title')}</h2>
			<p class="text-muted-foreground mt-3 max-w-[68ch] text-base leading-relaxed">
				{t('wp.s5_body')}
			</p>
			<figure class="mt-6 rounded-xl border bg-white p-3">
				<img
					class="w-full rounded-lg"
					src="/whitepaper/credit-lifecycle.png"
					alt={t('wp.fig3_cap')}
					loading="lazy"
				/>
				<figcaption class="pt-2 pl-1 text-xs text-slate-600">{t('wp.fig3_cap')}</figcaption>
			</figure>
		</section>

		<section id="s6" class="mb-14 scroll-mt-20">
			<h2 class="text-2xl font-semibold">{t('wp.s6_title')}</h2>
			<p class="text-muted-foreground mt-3 max-w-[68ch] text-base leading-relaxed">
				{t('wp.s6_body')}
			</p>
			<figure class="mt-6 rounded-xl border bg-white p-3">
				<img
					class="w-full rounded-lg"
					src="/whitepaper/failure-map.png"
					alt={t('wp.fig4_cap')}
					loading="lazy"
				/>
				<figcaption class="pt-2 pl-1 text-xs text-slate-600">{t('wp.fig4_cap')}</figcaption>
			</figure>
		</section>

		<section id="s7" class="scroll-mt-20">
			<h2 class="text-2xl font-semibold">{t('wp.s7_title')}</h2>
			<p class="text-muted-foreground mt-3 max-w-[68ch] text-base leading-relaxed">
				{t('wp.s7_body')}
			</p>
			<Button class="mt-6" variant="outline" href="{repoUrl}/tree/main/docs">
				{t('wp.cta_docs')}
			</Button>
		</section>
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
