<script lang="ts">
	import { resolve } from '$app/paths';
	import BrandMark from '$lib/components/brand-mark.svelte';
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import ModeToggle from './ModeToggle.svelte';
	import SiteFooter from './SiteFooter.svelte';
	import { t } from '$lib/i18n.svelte';
	import type { Snippet } from 'svelte';

	let {
		current,
		children
	}: {
		current?: 'whitepaper' | 'benchmark' | 'illusions';
		children: Snippet;
	} = $props();
</script>

<div class="landing-surface latent-page">
	<div class="canvas" aria-hidden="true">
		<LatentCanvas followCursor animate warmupFrames={1400} />
	</div>
	<div class="veil" aria-hidden="true"></div>

	<header>
		<a class="mark" href={resolve('/')}><BrandMark /></a>
		<nav aria-label={t('nav.primary')}>
			<a href={resolve('/whitepaper')} aria-current={current === 'whitepaper' ? 'page' : undefined}>
				{t('nav.whitepaper')}
			</a>
			<a href={resolve('/benchmark')} aria-current={current === 'benchmark' ? 'page' : undefined}>
				{t('nav.benchmark')}
			</a>
			<a href={resolve('/illusions')} aria-current={current === 'illusions' ? 'page' : undefined}>
				{t('nav.illusions')}
			</a>
		</nav>
		<div class="chrome-actions">
			<LanguageToggle />
			<ModeToggle />
			<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
		</div>
	</header>

	{@render children()}

	<SiteFooter />
</div>

<style>
	/* Hallmark - the latent chrome, reused by the document pages */
	.latent-page {
		position: relative;
		min-width: 0;
		min-height: 100svh;
		overflow-x: clip;
	}

	.canvas,
	.veil {
		position: fixed;
		inset: 0;
		z-index: 0;
	}

	.veil {
		background:
			radial-gradient(38% 46% at 26% 30%, oklch(0.62 0.2 255 / 18%) 0%, transparent 72%),
			radial-gradient(70% 60% at 40% 30%, transparent 0%, var(--k-veil) 88%);
		pointer-events: none;
	}

	/* Sticky, so the mark that leads home stays in reach on long pages. */
	header {
		position: sticky;
		inset-block-start: 0;
		z-index: 2;
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 1rem;
		padding: 1.1rem clamp(1rem, 3vw, 2.5rem);
		border-block-end: 1px solid var(--k-line);
		background: oklch(0.08 0.012 265 / 72%);
		backdrop-filter: blur(20px);
	}

	:global(:root[data-landing-mode='light']) header {
		background: oklch(0.97 0.004 255 / 78%);
	}

	.chrome-actions {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}

	.mark {
		color: var(--k-ink);
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.03em;
	}

	header nav {
		display: none;
		justify-content: center;
		gap: 1.75rem;
		color: var(--k-muted);
		font-size: 0.9rem;
	}

	header nav a:hover,
	header nav a[aria-current='page'] {
		color: var(--k-ink);
	}

	@media (min-width: 48rem) {
		header nav {
			display: flex;
		}
	}
</style>
