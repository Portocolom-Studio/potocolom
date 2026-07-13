<script lang="ts">
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import WaitlistForm from '$lib/components/WaitlistForm.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
</script>

<svelte:head>
	<title>potocolom</title>
	<meta name="description" content={t('hero.sub')} />
</svelte:head>

<header>
	<a class="logo" href={resolve('/')}>potocolom<span>_</span></a>
	<nav>
		<a href="#features">{t('nav.features')}</a>
		<a href="#pricing">{t('nav.pricing')}</a>
		<a href="#open">{t('nav.open')}</a>
		<a href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
	</nav>
	<div class="header-actions">
		<LanguageToggle />
		<a class="button primary small" href={resolve('/app')}>{t('nav.launch')}</a>
	</div>
</header>

<main>
	<section class="hero">
		<div class="hero-canvas"><LatentCanvas /></div>
		<div class="hero-content">
			<p class="kicker">{t('hero.kicker')}</p>
			<h1>
				{t('hero.title1')}<br />
				<span class="gradient-text">{t('hero.title2')}</span>
			</h1>
			<p class="sub">{t('hero.sub')}</p>
			<div class="cta-row">
				<a class="button primary" href={resolve('/app')}>{t('hero.cta_launch')}</a>
				<a class="button ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
			</div>
			<ul class="badges">
				<li>{t('hero.badge_license')}</li>
				<li>{t('hero.badge_fps')}</li>
				<li>{t('hero.badge_gpu')}</li>
			</ul>
		</div>
	</section>

	<WaitlistForm />

	<section id="features" class="band">
		<h2>{t('features.title')}</h2>
		<p class="band-sub">{t('features.sub')}</p>
		<div class="cards">
			<article>
				<h3>{t('features.f1_title')}</h3>
				<p>{t('features.f1_body')}</p>
			</article>
			<article>
				<h3>{t('features.f2_title')}</h3>
				<p>{t('features.f2_body')}</p>
			</article>
			<article>
				<h3>{t('features.f3_title')}</h3>
				<p>{t('features.f3_body')}</p>
			</article>
			<article>
				<h3>{t('features.f4_title')}</h3>
				<p>{t('features.f4_body')}</p>
			</article>
		</div>
	</section>

	<section id="pricing" class="band">
		<p class="kicker">{t('pricing.kicker')}</p>
		<h2>{t('pricing.title')}</h2>
		<p class="band-sub">{t('pricing.sub')}</p>
		<div class="tiers">
			<article>
				<h3>{t('pricing.t1_name')}</h3>
				<p class="price">&euro;9 <span>{t('pricing.month')}</span></p>
				<ul>
					<li>{t('pricing.t1_b1')}</li>
					<li>{t('pricing.t1_b2')}</li>
					<li>{t('pricing.t1_b3')}</li>
				</ul>
			</article>
			<article class="featured">
				<p class="badge">{t('pricing.t2_badge')}</p>
				<h3>{t('pricing.t2_name')}</h3>
				<p class="price">&euro;24 <span>{t('pricing.month')}</span></p>
				<ul>
					<li>{t('pricing.t2_b1')}</li>
					<li>{t('pricing.t2_b2')}</li>
					<li>{t('pricing.t2_b3')}</li>
				</ul>
			</article>
			<article>
				<h3>{t('pricing.t3_name')}</h3>
				<p class="price">&euro;59 <span>{t('pricing.month')}</span></p>
				<ul>
					<li>{t('pricing.t3_b1')}</li>
					<li>{t('pricing.t3_b2')}</li>
					<li>{t('pricing.t3_b3')}</li>
				</ul>
			</article>
		</div>
		<p class="trial-note">{t('pricing.trial')}</p>
	</section>

	<section id="open" class="band">
		<h2>{t('split.title')}</h2>
		<div class="split">
			<article>
				<h3>{t('split.oss_title')}</h3>
				<ul>
					<li>{t('split.oss_p1')}</li>
					<li>{t('split.oss_p2')}</li>
					<li>{t('split.oss_p3')}</li>
				</ul>
				<a class="button ghost small" href={repoUrl}>{t('footer.github')}</a>
			</article>
			<article class="highlight">
				<h3>{t('split.cloud_title')}</h3>
				<ul>
					<li>{t('split.cloud_p1')}</li>
					<li>{t('split.cloud_p2')}</li>
					<li>{t('split.cloud_p3')}</li>
				</ul>
				<a class="button primary small" href={resolve('/app')}>{t('nav.launch')}</a>
			</article>
		</div>
	</section>
</main>

<footer>
	<p>{t('footer.tagline')}</p>
	<nav>
		<a href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
		<a href={repoUrl}>{t('footer.github')}</a>
		<a href="{repoUrl}/tree/main/docs">{t('footer.docs')}</a>
		<a href={resolve('/legal')}>{t('footer.legal')}</a>
		<a href={resolve('/privacy')}>{t('footer.privacy')}</a>
		<a href="mailto:admin@leonfuller.com">{t('footer.contact')}</a>
	</nav>
</footer>

<style>
	header {
		position: fixed;
		inset: 0 0 auto;
		z-index: 10;
		display: flex;
		align-items: center;
		gap: 2rem;
		padding: 0.9rem clamp(1rem, 4vw, 3rem);
		background: rgba(7, 11, 20, 0.65);
		backdrop-filter: blur(12px);
		border-bottom: 1px solid var(--border);
	}

	.logo {
		font-weight: 700;
		font-size: 1.05rem;
		letter-spacing: -0.02em;
		text-decoration: none;
	}

	.logo span {
		color: var(--accent-2);
	}

	header nav {
		display: flex;
		gap: 1.4rem;
		margin-right: auto;
	}

	header nav a {
		color: var(--text-muted);
		text-decoration: none;
		font-size: 0.9rem;
	}

	header nav a:hover {
		color: var(--text);
	}

	.header-actions {
		display: flex;
		align-items: center;
		gap: 0.8rem;
	}

	@media (max-width: 640px) {
		header nav {
			display: none;
		}

		.header-actions {
			margin-left: auto;
		}
	}

	.hero {
		position: relative;
		min-height: 92vh;
		display: grid;
		place-items: center;
		overflow: hidden;
	}

	.hero-canvas {
		position: absolute;
		inset: 0;
	}

	.hero-canvas::after {
		content: '';
		position: absolute;
		inset: 0;
		background:
			radial-gradient(ellipse 75% 70% at 50% 45%, transparent 45%, var(--bg) 98%),
			linear-gradient(transparent 72%, var(--bg));
	}

	.hero-content {
		position: relative;
		text-align: center;
		padding: 7rem 1.5rem 4rem;
		max-width: 780px;
	}

	.kicker {
		color: var(--accent-2);
		font-size: 0.85rem;
		font-weight: 500;
		letter-spacing: 0.22em;
		text-transform: uppercase;
	}

	h1 {
		font-size: clamp(2.6rem, 7vw, 4.6rem);
		font-weight: 700;
		margin: 0.4em 0;
	}

	.gradient-text {
		background: var(--gradient-accent);
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
	}

	.sub {
		color: var(--text-muted);
		font-size: 1.1rem;
		max-width: 620px;
		margin: 0 auto 2rem;
	}

	.cta-row {
		display: flex;
		gap: 0.9rem;
		justify-content: center;
		flex-wrap: wrap;
	}

	.button {
		display: inline-block;
		padding: 0.75rem 1.5rem;
		border-radius: 999px;
		text-decoration: none;
		font-weight: 600;
		font-size: 0.95rem;
		transition:
			transform 0.15s ease,
			box-shadow 0.15s ease;
	}

	.button.small {
		padding: 0.45rem 1rem;
		font-size: 0.85rem;
	}

	.button.primary {
		background: var(--gradient-accent);
		color: #06080f;
		box-shadow: 0 0 24px rgba(124, 111, 255, 0.35);
	}

	.button.primary:hover {
		transform: translateY(-2px);
		box-shadow: 0 0 34px rgba(34, 211, 238, 0.4);
	}

	.button.ghost {
		border: 1px solid var(--border);
		color: var(--text);
		background: var(--surface);
	}

	.button.ghost:hover {
		background: var(--surface-hover);
	}

	.badges {
		display: flex;
		gap: 0.6rem;
		justify-content: center;
		flex-wrap: wrap;
		list-style: none;
		padding: 0;
		margin-top: 2.4rem;
	}

	.badges li {
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 0.3rem 0.9rem;
		font-size: 0.78rem;
		color: var(--text-muted);
		background: var(--surface);
	}

	.band {
		max-width: var(--content-width);
		margin: 0 auto;
		padding: 5.5rem clamp(1rem, 4vw, 3rem) 1rem;
	}

	.band h2 {
		font-size: clamp(1.7rem, 3.5vw, 2.4rem);
	}

	.band-sub {
		color: var(--text-muted);
		max-width: 560px;
		margin-bottom: 2.5rem;
	}

	.cards {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 1rem;
	}

	@media (max-width: 640px) {
		.cards {
			grid-template-columns: 1fr;
		}
	}

	.cards article {
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 1.6rem;
		background: linear-gradient(rgba(255, 255, 255, 0.045), rgba(255, 255, 255, 0.015));
		transition:
			transform 0.15s ease,
			border-color 0.15s ease;
	}

	.cards article:hover {
		transform: translateY(-3px);
		border-color: rgba(124, 111, 255, 0.45);
	}

	.cards h3 {
		font-size: 1.1rem;
	}

	.cards p {
		color: var(--text-muted);
		font-size: 0.95rem;
		margin: 0;
	}

	.tiers {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 1rem;
		align-items: start;
	}

	@media (max-width: 760px) {
		.tiers {
			grid-template-columns: 1fr;
		}
	}

	.tiers article {
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 1.8rem;
		background: var(--surface);
		position: relative;
	}

	.tiers article.featured {
		border-color: rgba(124, 111, 255, 0.5);
		background: linear-gradient(160deg, rgba(124, 111, 255, 0.12), rgba(34, 211, 238, 0.05));
	}

	.badge {
		position: absolute;
		top: -0.7rem;
		right: 1.2rem;
		background: var(--gradient-accent);
		color: #06080f;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		padding: 0.25rem 0.8rem;
		border-radius: 999px;
		margin: 0;
	}

	.tiers h3 {
		font-size: 1.05rem;
		color: var(--text-muted);
		margin-bottom: 0.2rem;
	}

	.price {
		font-size: 2.1rem;
		font-weight: 700;
		letter-spacing: -0.02em;
		margin: 0 0 1rem;
	}

	.price span {
		font-size: 0.85rem;
		font-weight: 500;
		color: var(--text-muted);
		letter-spacing: 0;
	}

	.tiers ul {
		list-style: none;
		padding: 0;
		margin: 0;
		color: var(--text-muted);
		font-size: 0.95rem;
	}

	.tiers li {
		padding: 0.35rem 0 0.35rem 1.4rem;
		position: relative;
	}

	.tiers li::before {
		content: '';
		position: absolute;
		left: 0;
		top: 0.85em;
		width: 0.55em;
		height: 0.55em;
		border-radius: 2px;
		background: var(--gradient-accent);
	}

	.trial-note {
		color: var(--text-muted);
		font-size: 0.9rem;
		margin-top: 1.6rem;
	}

	.split {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
		gap: 1rem;
		margin-top: 1.5rem;
	}

	.split article {
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 1.8rem;
		background: var(--surface);
	}

	.split article.highlight {
		border-color: rgba(124, 111, 255, 0.5);
		background: linear-gradient(160deg, rgba(124, 111, 255, 0.12), rgba(34, 211, 238, 0.05));
	}

	.split h3 {
		font-size: 1.2rem;
	}

	.split ul {
		list-style: none;
		padding: 0;
		margin: 0 0 1.4rem;
		color: var(--text-muted);
	}

	.split li {
		padding: 0.35rem 0 0.35rem 1.4rem;
		position: relative;
	}

	.split li::before {
		content: '';
		position: absolute;
		left: 0;
		top: 0.85em;
		width: 0.55em;
		height: 0.55em;
		border-radius: 2px;
		background: var(--gradient-accent);
	}

	footer {
		margin-top: 5rem;
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
