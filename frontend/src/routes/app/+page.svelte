<script lang="ts">
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';

	const tabs = ['draw', 'generate', 'edit', 'enhance'] as const;
	type Tab = (typeof tabs)[number];

	let active = $state<Tab>('draw');
</script>

<svelte:head>
	<title>potocolom - {t('app.title')}</title>
</svelte:head>

<div class="shell">
	<header>
		<a class="logo" href={resolve('/')} title={t('app.back')}>potocolom<span>_</span></a>
		<nav aria-label={t('app.title')}>
			{#each tabs as tab (tab)}
				<button
					type="button"
					class={{ active: active === tab }}
					aria-pressed={active === tab}
					onclick={() => (active = tab)}
				>
					{t(`app.tab_${tab}`)}
				</button>
			{/each}
		</nav>
		<LanguageToggle />
	</header>

	<main>
		{#if active === 'draw'}
			<div class="workspace">
				<div class="pane">
					<span class="pane-hint">{t('app.canvas_hint')}</span>
				</div>
				<div class="pane">
					<span class="pane-hint">{t('app.result_hint')}</span>
				</div>
			</div>
			<p class="placeholder">{t('app.draw_placeholder')}</p>
		{:else if active === 'generate'}
			<div class="workspace single">
				<div class="pane"></div>
			</div>
			<p class="placeholder">{t('app.generate_placeholder')}</p>
		{:else if active === 'edit'}
			<div class="workspace single">
				<div class="pane"></div>
			</div>
			<p class="placeholder">{t('app.edit_placeholder')}</p>
		{:else}
			<div class="workspace single">
				<div class="pane"></div>
			</div>
			<p class="placeholder">{t('app.enhance_placeholder')}</p>
		{/if}
	</main>
</div>

<style>
	.shell {
		min-height: 100vh;
		display: flex;
		flex-direction: column;
	}

	header {
		display: flex;
		align-items: center;
		gap: 1.5rem;
		padding: 0.8rem clamp(1rem, 3vw, 2rem);
		border-bottom: 1px solid var(--border);
		background: var(--bg-elevated);
	}

	.logo {
		font-weight: 700;
		text-decoration: none;
	}

	.logo span {
		color: var(--accent-2);
	}

	nav {
		display: flex;
		gap: 0.3rem;
		margin-right: auto;
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 3px;
	}

	nav button {
		border: none;
		background: none;
		color: var(--text-muted);
		padding: 0.42rem 1.1rem;
		border-radius: 999px;
		font-size: 0.9rem;
		font-weight: 500;
		cursor: pointer;
	}

	nav button.active {
		background: var(--gradient-accent);
		color: #06080f;
		font-weight: 600;
	}

	main {
		flex: 1;
		display: flex;
		flex-direction: column;
		padding: clamp(1rem, 3vw, 2rem);
		gap: 1rem;
	}

	.workspace {
		flex: 1;
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 1rem;
		min-height: 55vh;
	}

	.workspace.single {
		grid-template-columns: 1fr;
	}

	.pane {
		border: 1px dashed var(--border);
		border-radius: var(--radius);
		background:
			radial-gradient(ellipse 70% 60% at 50% 40%, rgba(124, 111, 255, 0.06), transparent),
			var(--surface);
		display: grid;
		place-items: center;
	}

	.pane-hint {
		color: var(--text-muted);
		font-size: 0.85rem;
		letter-spacing: 0.12em;
		text-transform: uppercase;
	}

	.placeholder {
		color: var(--text-muted);
		font-size: 0.92rem;
		margin: 0;
	}

	@media (max-width: 760px) {
		.workspace {
			grid-template-columns: 1fr;
		}

		nav {
			order: 3;
			width: 100%;
			justify-content: space-between;
		}

		header {
			flex-wrap: wrap;
		}
	}
</style>
