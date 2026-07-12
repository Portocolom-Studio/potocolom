<script lang="ts">
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import WaitlistForm from '$lib/components/WaitlistForm.svelte';
	import ForkTerminal from '$lib/components/ForkTerminal.svelte';
	import UserGenerationGallery from '$lib/components/UserGenerationGallery.svelte';
	import TextRotate from '$lib/components/TextRotate.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
	import * as Card from '$lib/components/ui/card';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import { Separator } from '$lib/components/ui/separator';
	import CheckIcon from '@lucide/svelte/icons/check';
	import GitForkIcon from '@lucide/svelte/icons/git-fork';
	import ArrowUpRightIcon from '@lucide/svelte/icons/arrow-up-right';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const forkUrl = `${repoUrl}/fork`;

	const features = ['f1', 'f2', 'f3', 'f4'] as const;
	const points = ['p1', 'p2', 'p3'] as const;
	const forkPoints = ['b1', 'b2', 'b3'] as const;
	const tiers = [
		{ key: 't1', price: '9', featured: false, bullets: ['b1', 'b2', 'b3'] },
		{ key: 't2', price: '24', featured: true, bullets: ['b1', 'b2', 'b3'] },
		{ key: 't3', price: '59', featured: false, bullets: ['b1', 'b2', 'b3'] }
	] as const;

	const galleryWords = $derived([
		{
			label: t('gallery.word_making'),
			class: 'rounded-md bg-primary/20 px-2 py-0.5 text-primary leading-none'
		},
		{
			label: t('gallery.word_designing'),
			class: 'rounded-md bg-chart-2/20 px-2 py-0.5 text-chart-2 leading-none'
		},
		{
			label: t('gallery.word_creating'),
			class: 'rounded-md bg-chart-3/20 px-2 py-0.5 text-chart-3 leading-none'
		}
	]);
</script>

<svelte:head>
	<title>potocolom</title>
	<meta name="description" content={t('hero.sub')} />
</svelte:head>

<header class="bg-background/70 fixed inset-x-0 top-0 z-50 border-b backdrop-blur-md">
	<div class="mx-auto flex h-16 max-w-6xl items-center gap-6 px-4 sm:px-6">
		<a class="text-base font-bold" href={resolve('/')}>
			potocolom<span class="text-primary">_</span>
		</a>
		<nav class="text-muted-foreground hidden gap-6 text-base md:flex">
			<a class="hover:text-foreground transition-colors" href="#features">{t('nav.features')}</a>
			<a class="hover:text-foreground transition-colors" href="#pricing">{t('nav.pricing')}</a>
			<a class="hover:text-foreground transition-colors" href="#open">{t('nav.open')}</a>
			<a class="hover:text-foreground transition-colors" href={resolve('/whitepaper')}>
				{t('nav.whitepaper')}
			</a>
		</nav>
		<div class="ml-auto flex items-center gap-3">
			<LanguageToggle />
			<Button size="sm" variant="gradient" href={resolve('/app')}>{t('nav.launch')}</Button>
		</div>
	</div>
</header>

<main>
	<section class="relative grid min-h-[92vh] place-items-center overflow-hidden">
		<div class="hero-canvas absolute inset-0">
			<img
				src="/hero-poster.png"
				alt=""
				class="absolute inset-0 size-full object-cover"
				decoding="async"
				fetchpriority="high"
			/>
			<div class="absolute inset-0"><LatentCanvas /></div>
		</div>
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
				<Button size="lg" variant="gradient" href={resolve('/app')}>{t('hero.cta_launch')}</Button>
				<Button size="lg" variant="outline" href={repoUrl}>{t('hero.cta_selfhost')}</Button>
			</div>
			<div class="mt-10 flex flex-wrap justify-center gap-2.5">
				<Badge variant="surface">{t('hero.badge_license')}</Badge>
				<Badge variant="surface">{t('hero.badge_fps')}</Badge>
				<Badge variant="surface">{t('hero.badge_gpu')}</Badge>
			</div>
		</div>
	</section>

	<WaitlistForm />

	<section id="features" class="mx-auto max-w-6xl scroll-mt-20 px-4 py-24 sm:px-6">
		<h2 class="text-3xl font-semibold">{t('features.title')}</h2>
		<p class="text-muted-foreground mt-4 max-w-2xl text-base leading-relaxed">
			{t('features.sub')}
		</p>
		<div class="mt-12 grid grid-cols-1 gap-8 sm:grid-cols-2">
			{#each features as feature (feature)}
				<Card.Root class="h-full [--card-spacing:--spacing(6)]">
					<Card.Header class="gap-4">
						<Card.Title class="text-xl leading-snug">{t(`features.${feature}_title`)}</Card.Title>
						<Card.Description class="text-base leading-relaxed">
							{t(`features.${feature}_body`)}
						</Card.Description>
					</Card.Header>
				</Card.Root>
			{/each}
		</div>
	</section>

	<section id="gallery" class="mx-auto max-w-6xl scroll-mt-20 px-4 py-24 sm:px-6">
		<p class="text-primary text-xs font-semibold tracking-[0.2em] uppercase">
			{t('gallery.kicker')}
		</p>
		<h2 class="mt-2 flex flex-wrap items-baseline gap-x-2 text-3xl font-semibold">
			<span>{t('gallery.title_before')}</span>
			<TextRotate words={galleryWords} />
			<span class="sr-only">
				{t('gallery.word_making')}, {t('gallery.word_designing')}, {t('gallery.word_creating')}
			</span>
		</h2>
		<p class="text-muted-foreground mt-3 max-w-2xl text-base leading-relaxed">{t('gallery.sub')}</p>
		<div class="mt-10">
			<UserGenerationGallery />
		</div>
	</section>

	<section id="pricing" class="mx-auto max-w-6xl scroll-mt-20 px-4 py-24 sm:px-6">
		<p class="text-primary text-xs font-semibold tracking-[0.2em] uppercase">
			{t('pricing.kicker')}
		</p>
		<h2 class="mt-2 text-3xl font-semibold">{t('pricing.title')}</h2>
		<p class="text-muted-foreground mt-3 max-w-2xl text-base leading-relaxed">{t('pricing.sub')}</p>
		<div class="mt-10 grid grid-cols-1 gap-6 md:grid-cols-3">
			{#each tiers as tier (tier.key)}
				<div
					class:h-full={true}
					class:pricing-aura={tier.featured}
					class:pricing-aura-rainbow={tier.featured}
				>
					<Card.Root class="h-full">
						<Card.Header>
							<div class="flex items-center justify-between gap-3">
								<Card.Description>{t(`pricing.${tier.key}_name`)}</Card.Description>
								{#if tier.featured}
									<Badge>{t('pricing.t2_badge')}</Badge>
								{/if}
							</div>
							<Card.Title class="text-4xl">
								&euro;{tier.price}
								<span class="text-muted-foreground text-base font-normal">{t('pricing.month')}</span
								>
							</Card.Title>
						</Card.Header>
						<Card.Content>
							<ul class="flex flex-col gap-3 text-base leading-relaxed">
								{#each tier.bullets as bullet (bullet)}
									<li class="text-muted-foreground flex items-start gap-2">
										<CheckIcon class="text-primary mt-0.5 size-4 shrink-0" />
										{t(`pricing.${tier.key}_${bullet}`)}
									</li>
								{/each}
							</ul>
						</Card.Content>
					</Card.Root>
				</div>
			{/each}
		</div>
		<p class="text-muted-foreground mt-8 text-base leading-relaxed">{t('pricing.trial')}</p>
	</section>

	<section id="open" class="mx-auto max-w-6xl scroll-mt-20 px-4 py-24 sm:px-6">
		<h2 class="max-w-2xl text-3xl font-semibold">{t('split.title')}</h2>
		<div class="mt-10 grid grid-cols-1 gap-6 md:grid-cols-2">
			<Card.Root class="h-full">
				<Card.Header>
					<Card.Title class="text-xl">{t('split.oss_title')}</Card.Title>
				</Card.Header>
				<Card.Content>
					<ul class="flex flex-col gap-3 text-base leading-relaxed">
						{#each points as point (point)}
							<li class="text-muted-foreground flex items-start gap-2">
								<CheckIcon class="text-primary mt-0.5 size-4 shrink-0" />
								{t(`split.oss_${point}`)}
							</li>
						{/each}
					</ul>
				</Card.Content>
				<Card.Footer>
					<Button variant="outline" href={repoUrl}>{t('footer.github')}</Button>
				</Card.Footer>
			</Card.Root>
			<Card.Root class="border-primary/50 h-full">
				<Card.Header>
					<Card.Title class="text-xl">{t('split.cloud_title')}</Card.Title>
				</Card.Header>
				<Card.Content>
					<ul class="flex flex-col gap-3 text-base leading-relaxed">
						{#each points as point (point)}
							<li class="text-muted-foreground flex items-start gap-2">
								<CheckIcon class="text-primary mt-0.5 size-4 shrink-0" />
								{t(`split.cloud_${point}`)}
							</li>
						{/each}
					</ul>
				</Card.Content>
				<Card.Footer>
					<Button variant="gradient" href={resolve('/app')}>{t('nav.launch')}</Button>
				</Card.Footer>
			</Card.Root>
		</div>
	</section>

	<section id="fork" class="mx-auto max-w-6xl scroll-mt-20 px-4 py-24 sm:px-6">
		<h2 class="text-4xl font-semibold tracking-tight sm:text-5xl">{t('fork.title')}</h2>
		<div
			class="mt-10 grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,0.88fr)_minmax(0,1.12fr)] lg:items-stretch"
		>
			<div
				class="border-border flex h-full min-h-[22rem] flex-col rounded-xl border p-6 sm:p-8 lg:min-h-0"
			>
				<ul class="flex flex-1 flex-col gap-4 text-base leading-relaxed">
					{#each forkPoints as point (point)}
						<li class="text-muted-foreground flex items-start gap-2">
							<CheckIcon class="text-primary mt-0.5 size-4 shrink-0" />
							{t(`fork.${point}`)}
						</li>
					{/each}
				</ul>
				<div class="mt-8 flex flex-wrap items-center gap-3">
					<Button variant="light" href={forkUrl}>
						<GitForkIcon class="size-4" />
						{t('fork.cta_fork')}
					</Button>
					<Button variant="outline" href={repoUrl}>
						{t('fork.cta_source')}
						<ArrowUpRightIcon class="size-3.5 opacity-70" />
					</Button>
				</div>
			</div>
			<ForkTerminal />
		</div>
	</section>
</main>

<Separator />

<footer
	class="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-4 py-8 sm:px-6"
>
	<p class="text-muted-foreground text-base leading-relaxed">{t('footer.tagline')}</p>
	<nav class="text-muted-foreground flex flex-wrap gap-6 text-base">
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

	/* DaisyUI-style aura rainbow on the featured pricing tier */
	@property --aura-angle {
		syntax: '<angle>';
		inherits: false;
		initial-value: 0deg;
	}

	.pricing-aura {
		--aura-padding: 0.125rem;
		--aura-radius: var(--radius-xl);
		position: relative;
		display: block;
		padding: var(--aura-padding);
		border-radius: calc(var(--aura-padding) + var(--aura-radius));
		animation: pricing-aura 6s linear infinite;
		background-image: conic-gradient(from var(--aura-angle), transparent 225deg, currentColor);
	}

	.pricing-aura > :global(*) {
		position: relative;
		z-index: 1;
	}

	.pricing-aura::before,
	.pricing-aura::after {
		content: '';
		position: absolute;
		top: 50%;
		left: 50%;
		z-index: 0;
		display: block;
		width: 100%;
		height: 100%;
		border-radius: inherit;
		background-color: inherit;
		background-image: inherit;
		translate: -50% -50%;
		opacity: 0.7;
		filter: blur(0.25rem);
		animation: inherit;
	}

	.pricing-aura::after {
		opacity: 0.3;
		filter: blur(1rem);
	}

	.pricing-aura-rainbow {
		background: conic-gradient(
			from var(--aura-angle) in oklch longer hue,
			transparent 10%,
			oklch(80% 0.15 0deg),
			oklch(80% 0.15 360deg),
			transparent 90%
		);
	}

	@keyframes pricing-aura {
		to {
			--aura-angle: 360deg;
			transform: translateZ(1px);
		}
	}
</style>
