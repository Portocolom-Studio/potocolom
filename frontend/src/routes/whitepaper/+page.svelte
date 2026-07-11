<script lang="ts">
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';

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
	<meta property="og:type" content="article" />
	<meta property="og:site_name" content="potocolom" />
	<meta property="og:title" content="potocolom - {t('wp.title')}" />
	<meta property="og:description" content={t('wp.sub')} />
	<meta property="og:image" content="https://potocolom.com/og.png" />
	<meta name="twitter:card" content="summary_large_image" />
</svelte:head>

<header>
	<a class="logo" href={resolve('/')}>potocolom<span>_</span></a>
	<div class="header-actions">
		<LanguageToggle />
		<a class="button primary small" href={resolve('/app')}>{t('nav.launch')}</a>
	</div>
</header>

<div class="intro">
	<p class="kicker">{t('wp.kicker')}</p>
	<h1>{t('wp.title')}</h1>
	<p class="sub">{t('wp.sub')}</p>
</div>

<div class="layout">
	<aside aria-label={t('wp.toc')}>
		<p class="toc-title">{t('wp.toc')}</p>
		<ol>
			{#each sections as section (section.id)}
				<li><a href="#{section.id}">{t(section.title)}</a></li>
			{/each}
		</ol>
	</aside>

	<article>
		<section id="s1">
			<h2>{t('wp.s1_title')}</h2>
			<p>{t('wp.s1_body')}</p>
			<figure>
				<img src="/whitepaper/platform-overview.png" alt={t('wp.fig1_cap')} loading="lazy" />
				<figcaption>{t('wp.fig1_cap')}</figcaption>
			</figure>
		</section>

		<section id="s2">
			<h2>{t('wp.s2_title')}</h2>
			<p>{t('wp.s2_body')}</p>
			<figure>
				<img src="/whitepaper/realtime-loop.png" alt={t('wp.fig2_cap')} loading="lazy" />
				<figcaption>{t('wp.fig2_cap')}</figcaption>
			</figure>
		</section>

		<section id="s3">
			<h2>{t('wp.s3_title')}</h2>
			<p>{t('wp.s3_body')}</p>
		</section>

		<section id="s4">
			<h2>{t('wp.s4_title')}</h2>
			<p>{t('wp.s4_body')}</p>
		</section>

		<section id="s5">
			<h2>{t('wp.s5_title')}</h2>
			<p>{t('wp.s5_body')}</p>
			<figure>
				<img src="/whitepaper/credit-lifecycle.png" alt={t('wp.fig3_cap')} loading="lazy" />
				<figcaption>{t('wp.fig3_cap')}</figcaption>
			</figure>
		</section>

		<section id="s6">
			<h2>{t('wp.s6_title')}</h2>
			<p>{t('wp.s6_body')}</p>
			<figure>
				<img src="/whitepaper/failure-map.png" alt={t('wp.fig4_cap')} loading="lazy" />
				<figcaption>{t('wp.fig4_cap')}</figcaption>
			</figure>
		</section>

		<section id="s7">
			<h2>{t('wp.s7_title')}</h2>
			<p>{t('wp.s7_body')}</p>
			<a class="button ghost" href="{repoUrl}/tree/main/docs">{t('wp.cta_docs')}</a>
		</section>
	</article>
</div>

<footer>
	<p>{t('footer.tagline')}</p>
	<nav>
		<a href={repoUrl}>{t('footer.github')}</a>
		<a href="{repoUrl}/tree/main/docs">{t('footer.docs')}</a>
		<a href={resolve('/legal')}>{t('footer.legal')}</a>
		<a href={resolve('/privacy')}>{t('footer.privacy')}</a>
		<a href="mailto:hello@potocolom.com">{t('footer.contact')}</a>
	</nav>
</footer>

<style>
	header {
		position: sticky;
		top: 0;
		z-index: 10;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1.5rem;
		padding: 0.9rem clamp(1rem, 4vw, 3rem);
		background: rgba(7, 11, 20, 0.75);
		backdrop-filter: blur(12px);
		border-bottom: 1px solid var(--border);
	}

	.logo {
		font-weight: 700;
		font-size: 1.05rem;
		text-decoration: none;
	}

	.logo span {
		color: var(--accent-2);
	}

	.header-actions {
		display: flex;
		align-items: center;
		gap: 0.8rem;
	}

	.button {
		display: inline-block;
		padding: 0.75rem 1.5rem;
		border-radius: 999px;
		text-decoration: none;
		font-weight: 600;
		font-size: 0.95rem;
	}

	.button.small {
		padding: 0.45rem 1rem;
		font-size: 0.85rem;
	}

	.button.primary {
		background: var(--gradient-accent);
		color: #06080f;
	}

	.button.ghost {
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
	}

	.button.ghost:hover {
		background: var(--surface-hover);
	}

	.intro {
		max-width: var(--content-width);
		margin: 0 auto;
		padding: 4.5rem clamp(1rem, 4vw, 3rem) 1rem;
	}

	.kicker {
		color: var(--accent-2);
		font-size: 0.85rem;
		font-weight: 500;
		letter-spacing: 0.22em;
		text-transform: uppercase;
		margin-bottom: 0.6rem;
	}

	h1 {
		font-size: clamp(2rem, 5vw, 3.2rem);
		font-weight: 700;
	}

	.sub {
		color: var(--text-muted);
		font-size: 1.05rem;
		max-width: 720px;
	}

	.layout {
		max-width: var(--content-width);
		margin: 0 auto;
		padding: 1.5rem clamp(1rem, 4vw, 3rem) 3rem;
		display: grid;
		grid-template-columns: 220px 1fr;
		gap: 3rem;
		align-items: start;
	}

	aside {
		position: sticky;
		top: 5.2rem;
	}

	.toc-title {
		font-size: 0.75rem;
		letter-spacing: 0.18em;
		text-transform: uppercase;
		color: var(--text-muted);
		margin-bottom: 0.6rem;
	}

	aside ol {
		list-style: none;
		margin: 0;
		padding: 0;
		border-left: 1px solid var(--border);
	}

	aside li a {
		display: block;
		padding: 0.4rem 0 0.4rem 1rem;
		color: var(--text-muted);
		text-decoration: none;
		font-size: 0.88rem;
		border-left: 2px solid transparent;
		margin-left: -1px;
	}

	aside li a:hover {
		color: var(--text);
		border-left-color: var(--accent-2);
	}

	section {
		scroll-margin-top: 5.5rem;
		margin-bottom: 3.5rem;
	}

	h2 {
		font-size: 1.55rem;
	}

	article p {
		color: var(--text-muted);
		max-width: 68ch;
	}

	figure {
		margin: 1.6rem 0 0;
		background: #f8fafc;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0.9rem;
	}

	figure img {
		display: block;
		width: 100%;
		height: auto;
		border-radius: var(--radius-sm);
	}

	figcaption {
		color: #475569;
		font-size: 0.82rem;
		padding: 0.7rem 0.3rem 0.1rem;
	}

	@media (max-width: 860px) {
		.layout {
			grid-template-columns: 1fr;
			gap: 1rem;
		}

		aside {
			position: static;
		}
	}

	footer {
		border-top: 1px solid var(--border);
		padding: 2rem clamp(1rem, 4vw, 3rem);
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 1rem;
		flex-wrap: wrap;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	footer p {
		margin: 0;
	}

	footer nav {
		display: flex;
		gap: 1.4rem;
	}

	footer a {
		color: var(--text-muted);
	}

	footer a:hover {
		color: var(--text);
	}
</style>
