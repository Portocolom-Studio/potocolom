<script lang="ts">
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import WaitlistForm from '$lib/components/WaitlistForm.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
	import * as Card from '$lib/components/ui/card';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import { Separator } from '$lib/components/ui/separator';
	import CheckIcon from '@lucide/svelte/icons/check';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';

	const features = ['f1', 'f2', 'f3', 'f4'] as const;
	const points = ['p1', 'p2', 'p3'] as const;
	const tiers = [
		{ key: 't1', price: '9', featured: false, bullets: ['b1', 'b2', 'b3'] },
		{ key: 't2', price: '24', featured: true, bullets: ['b1', 'b2', 'b3'] },
		{ key: 't3', price: '59', featured: false, bullets: ['b1', 'b2', 'b3'] }
	] as const;
</script>

<svelte:head>
	<title>potocolom</title>
	<meta name="description" content={t('hero.sub')} />
</svelte:head>

<header class="bg-background/70 fixed inset-x-0 top-0 z-50 border-b backdrop-blur-md">
	<div class="mx-auto flex h-14 max-w-6xl items-center gap-6 px-4 sm:px-6">
		<a class="text-base font-bold tracking-tight" href={resolve('/')}>
			potocolom<span class="text-primary">_</span>
		</a>
		<nav class="text-muted-foreground hidden gap-5 text-sm md:flex">
			<a class="hover:text-foreground transition-colors" href="#features">{t('nav.features')}</a>
			<a class="hover:text-foreground transition-colors" href="#pricing">{t('nav.pricing')}</a>
			<a class="hover:text-foreground transition-colors" href="#open">{t('nav.open')}</a>
			<a class="hover:text-foreground transition-colors" href={resolve('/whitepaper')}>
				{t('nav.whitepaper')}
			</a>
		</nav>
		<div class="ml-auto flex items-center gap-3">
			<LanguageToggle />
			<Button size="sm" href={resolve('/app')}>{t('nav.launch')}</Button>
		</div>
	</div>
</header>

<main>
	<section class="relative grid min-h-[92vh] place-items-center overflow-hidden">
		<div class="hero-canvas absolute inset-0"><LatentCanvas /></div>
		<div class="relative max-w-3xl px-6 pt-28 pb-16 text-center">
			<p class="text-primary text-xs font-semibold tracking-[0.22em] uppercase sm:text-sm">
				{t('hero.kicker')}
			</p>
			<h1 class="mt-4 text-5xl font-bold tracking-tight text-balance sm:text-6xl lg:text-7xl">
				{t('hero.title1')}<br />
				<span class="bg-clip-text text-transparent [background-image:var(--gradient-accent)]">
					{t('hero.title2')}
				</span>
			</h1>
			<p class="text-muted-foreground mx-auto mt-6 max-w-xl text-lg">{t('hero.sub')}</p>
			<div class="mt-8 flex flex-wrap items-center justify-center gap-3">
				<Button size="lg" href={resolve('/app')}>{t('hero.cta_launch')}</Button>
				<Button size="lg" variant="outline" href={repoUrl}>{t('hero.cta_selfhost')}</Button>
			</div>
			<div class="mt-10 flex flex-wrap justify-center gap-2">
				<Badge variant="outline">{t('hero.badge_license')}</Badge>
				<Badge variant="outline">{t('hero.badge_fps')}</Badge>
				<Badge variant="outline">{t('hero.badge_gpu')}</Badge>
			</div>
		</div>
	</section>

	<WaitlistForm />

	<section id="features" class="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
		<h2 class="text-3xl font-semibold tracking-tight">{t('features.title')}</h2>
		<p class="text-muted-foreground mt-2 max-w-xl">{t('features.sub')}</p>
		<div class="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-2">
			{#each features as feature (feature)}
				<Card.Root>
					<Card.Header>
						<Card.Title>{t(`features.${feature}_title`)}</Card.Title>
						<Card.Description>{t(`features.${feature}_body`)}</Card.Description>
					</Card.Header>
				</Card.Root>
			{/each}
		</div>
	</section>

	<section id="pricing" class="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
		<p class="text-primary text-xs font-semibold tracking-[0.2em] uppercase">
			{t('pricing.kicker')}
		</p>
		<h2 class="mt-2 text-3xl font-semibold tracking-tight">{t('pricing.title')}</h2>
		<p class="text-muted-foreground mt-2 max-w-2xl">{t('pricing.sub')}</p>
		<div class="mt-8 grid grid-cols-1 items-start gap-4 md:grid-cols-3">
			{#each tiers as tier (tier.key)}
				<Card.Root class={tier.featured ? 'border-primary relative' : 'relative'}>
					{#if tier.featured}
						<Badge class="absolute -top-3 right-4">{t('pricing.t2_badge')}</Badge>
					{/if}
					<Card.Header>
						<Card.Description>{t(`pricing.${tier.key}_name`)}</Card.Description>
						<Card.Title class="text-3xl">
							&euro;{tier.price}
							<span class="text-muted-foreground text-sm font-normal">{t('pricing.month')}</span>
						</Card.Title>
					</Card.Header>
					<Card.Content>
						<ul class="flex flex-col gap-2.5 text-sm">
							{#each tier.bullets as bullet (bullet)}
								<li class="text-muted-foreground flex items-start gap-2">
									<CheckIcon class="text-primary mt-0.5 size-4 shrink-0" />
									{t(`pricing.${tier.key}_${bullet}`)}
								</li>
							{/each}
						</ul>
					</Card.Content>
				</Card.Root>
			{/each}
		</div>
		<p class="text-muted-foreground mt-6 text-sm">{t('pricing.trial')}</p>
	</section>

	<section id="open" class="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
		<h2 class="max-w-2xl text-3xl font-semibold tracking-tight">{t('split.title')}</h2>
		<div class="mt-8 grid grid-cols-1 gap-4 md:grid-cols-2">
			<Card.Root>
				<Card.Header>
					<Card.Title>{t('split.oss_title')}</Card.Title>
				</Card.Header>
				<Card.Content>
					<ul class="flex flex-col gap-2.5 text-sm">
						{#each points as point (point)}
							<li class="text-muted-foreground flex items-start gap-2">
								<CheckIcon class="text-primary mt-0.5 size-4 shrink-0" />
								{t(`split.oss_${point}`)}
							</li>
						{/each}
					</ul>
				</Card.Content>
				<Card.Footer>
					<Button variant="outline" size="sm" href={repoUrl}>{t('footer.github')}</Button>
				</Card.Footer>
			</Card.Root>
			<Card.Root class="border-primary/50">
				<Card.Header>
					<Card.Title>{t('split.cloud_title')}</Card.Title>
				</Card.Header>
				<Card.Content>
					<ul class="flex flex-col gap-2.5 text-sm">
						{#each points as point (point)}
							<li class="text-muted-foreground flex items-start gap-2">
								<CheckIcon class="text-primary mt-0.5 size-4 shrink-0" />
								{t(`split.cloud_${point}`)}
							</li>
						{/each}
					</ul>
				</Card.Content>
				<Card.Footer>
					<Button size="sm" href={resolve('/app')}>{t('nav.launch')}</Button>
				</Card.Footer>
			</Card.Root>
		</div>
	</section>
</main>

<Separator />

<footer
	class="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-4 py-8 sm:px-6"
>
	<p class="text-muted-foreground text-sm">{t('footer.tagline')}</p>
	<nav class="text-muted-foreground flex flex-wrap gap-5 text-sm">
		<a class="hover:text-foreground transition-colors" href={resolve('/whitepaper')}>
			{t('nav.whitepaper')}
		</a>
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

<style>
	/* the kept flourish: fade the particle field into the page background */
	.hero-canvas::after {
		content: '';
		position: absolute;
		inset: 0;
		background:
			radial-gradient(ellipse 75% 70% at 50% 45%, transparent 45%, var(--background) 98%),
			linear-gradient(transparent 72%, var(--background));
	}
</style>
