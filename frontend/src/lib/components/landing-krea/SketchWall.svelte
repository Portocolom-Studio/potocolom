<script lang="ts">
	import { resolve } from '$app/paths';
	import HeroImageField from '$lib/components/HeroImageField.svelte';
	import { promptMarqueePrompts } from '$lib/prompt-marquee-prompts';
	import { t } from '$lib/i18n.svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';

	type Panel = 'pricing' | 'selfhost' | null;
	let panel = $state<Panel>(null);

	const plans = [
		{ key: 't1', credits: '10,000' },
		{ key: 't2', credits: '35,000' },
		{ key: 't3', credits: '100,000' }
	] as const;
</script>

<svelte:window onkeydown={(event) => event.key === 'Escape' && (panel = null)} />

<div class="krea wall">
	<div class="field" aria-hidden="true"><HeroImageField /></div>
	<div class="veil" aria-hidden="true"></div>

	<header>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<nav aria-label={t('nav.features')}>
			<button type="button" onclick={() => (panel = 'pricing')}>{t('nav.pricing')}</button>
			<button type="button" onclick={() => (panel = 'selfhost')}>{t('nav.open')}</button>
			<a href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
		</nav>
		<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
	</header>

	<div class="stage">
		<h1>{t('hero.title1')} {t('hero.title2')}</h1>
		<p>{t('hero.sub')}</p>
		<div class="actions">
			<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
			<a class="pill pill-ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
		</div>
	</div>

	<div class="ticker" aria-label={t('gallery.prompts_aria')}>
		<div class="ticker-track">
			{#each [...promptMarqueePrompts, ...promptMarqueePrompts] as prompt, index (index)}
				<span>{prompt.primary}</span>
			{/each}
		</div>
	</div>

	{#if panel}
		<div class="sheet" role="dialog" aria-modal="true" aria-label={t('nav.pricing')}>
			<button class="sheet-close" type="button" onclick={() => (panel = null)}>esc</button>
			{#if panel === 'pricing'}
				<h2>{t('pricing.title')}</h2>
				<dl>
					<div>
						<dt>{t('split.oss_title')}</dt>
						<dd class="num">0</dd>
						<dd>{t('split.oss_p1')}</dd>
					</div>
					{#each plans as plan (plan.key)}
						<div>
							<dt>{t(`pricing.${plan.key}_name`)}</dt>
							<dd class="num">{plan.credits}</dd>
							<dd>{t(`pricing.${plan.key}_b2`)}</dd>
						</div>
					{/each}
				</dl>
				<p class="note">{t('pricing.trial')}</p>
			{:else}
				<h2>{t('fork.title')}</h2>
				<pre><code
						>{t('fork.cmd1')}
{t('fork.cmd2')}
{t('fork.cmd3')}</code
					></pre>
				<p class="note">{t('fork.b3')}</p>
				<a class="pill pill-accent" href={repoUrl}>{t('fork.cta_source')}</a>
			{/if}
		</div>
	{/if}
</div>

<style>
	/* Hallmark - macrostructure: Single-Screen Takeover - genre: image-first - enrichment: live hero field as the whole page - nav: floating - contrast: pass - mobile: pass */
	.wall {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		grid-template-rows: auto 1fr auto;
		height: 100svh;
		overflow: clip;
	}

	.field,
	.veil {
		position: absolute;
		inset: 0;
	}

	.veil {
		background:
			radial-gradient(52% 42% at 50% 48%, var(--k-paper) 0%, var(--k-veil) 45%, transparent 78%),
			linear-gradient(
				to bottom,
				var(--k-veil) 0%,
				transparent 22%,
				transparent 70%,
				var(--k-veil) 100%
			);
	}

	header,
	.stage,
	.ticker {
		position: relative;
		z-index: 2;
	}

	header {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 1rem;
		padding: 1.1rem clamp(1rem, 3vw, 2.5rem);
	}

	.mark {
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

	header nav button {
		border: 0;
		background: none;
		color: inherit;
		cursor: pointer;
		font: inherit;
		white-space: nowrap;
	}

	header nav button:hover,
	header nav a:hover {
		color: var(--k-ink);
	}

	.stage {
		display: grid;
		align-content: center;
		justify-items: center;
		gap: 1.25rem;
		padding-inline: clamp(1rem, 4vw, 3rem);
		text-align: center;
	}

	h1 {
		max-width: 16ch;
		font-size: clamp(2.8rem, 7.5vw, 6.5rem);
		line-height: 0.95;
	}

	.stage p {
		max-width: 44ch;
		color: var(--k-muted);
		font-size: clamp(0.95rem, 1.4vw, 1.1rem);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 0.7rem;
	}

	.ticker {
		min-width: 0;
		overflow: clip;
		padding-block: 0.9rem;
		border-top: 1px solid var(--k-line);
		mask-image: linear-gradient(to right, transparent, black 8%, black 92%, transparent);
	}

	.ticker-track {
		display: flex;
		width: max-content;
		gap: 2.5rem;
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.78rem;
		white-space: nowrap;
		animation: drift 90s linear infinite;
	}

	.ticker:hover .ticker-track {
		animation-play-state: paused;
	}

	@keyframes drift {
		to {
			transform: translateX(-50%);
		}
	}

	.sheet {
		position: absolute;
		inset-block-end: 0;
		inset-inline: 0;
		z-index: 5;
		display: grid;
		gap: 1.1rem;
		max-height: 82svh;
		overflow-y: auto;
		padding: clamp(1.5rem, 4vw, 2.5rem);
		border-top: 1px solid var(--k-line);
		background: var(--k-panel);
		backdrop-filter: blur(20px);
	}

	.sheet h2 {
		font-size: clamp(1.6rem, 3vw, 2.4rem);
	}

	.sheet-close {
		justify-self: end;
		padding: 0.3rem 0.7rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: none;
		color: var(--k-muted);
		cursor: pointer;
		font-family: var(--k-mono);
		font-size: 0.7rem;
	}

	.sheet dl {
		margin: 0;
	}

	.sheet dl > div {
		display: grid;
		grid-template-columns: minmax(6rem, 0.4fr) minmax(4rem, 0.3fr) minmax(0, 1fr);
		gap: 0.35rem 1.5rem;
		align-items: baseline;
		padding-block: 0.9rem;
		border-top: 1px solid var(--k-line);
	}

	.sheet dt {
		font-weight: 700;
	}

	.sheet dd {
		margin: 0;
		color: var(--k-muted);
	}

	.num {
		color: var(--k-ink);
		font-family: var(--k-mono);
		font-variant-numeric: tabular-nums;
	}

	.sheet pre {
		margin: 0;
		overflow-x: auto;
		color: var(--k-ink);
		font-family: var(--k-mono);
		font-size: 0.78rem;
		line-height: 1.9;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.note {
		max-width: 60ch;
		color: var(--k-muted);
		font-size: 0.85rem;
	}

	.sheet .pill {
		justify-self: start;
	}

	@media (min-width: 48rem) {
		header nav {
			display: flex;
		}

		.sheet {
			inset-inline: auto 0;
			inset-block: 0;
			width: min(34rem, 100%);
			max-height: none;
			border-top: 0;
			border-inline-start: 1px solid var(--k-line);
			align-content: center;
		}

		.sheet dl > div {
			grid-template-columns: minmax(7rem, 0.4fr) minmax(5rem, 0.3fr) minmax(0, 1fr);
		}
	}

	@media (max-width: 30rem) {
		.actions {
			width: 100%;
			flex-direction: column;
		}

		.actions .pill {
			width: 100%;
		}
	}
</style>
