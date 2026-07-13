<script lang="ts">
	import { getLocale, setLocale, locales, t } from '$lib/i18n.svelte';
</script>

<div role="group" aria-label={t('ui.language')}>
	{#each locales as locale (locale)}
		<button
			type="button"
			class={{ active: getLocale() === locale }}
			aria-pressed={getLocale() === locale}
			onclick={() => setLocale(locale)}
		>
			{locale.toUpperCase()}
		</button>
	{/each}
</div>

<style>
	div {
		display: inline-flex;
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 2px;
		gap: 2px;
	}

	button {
		border: none;
		background: none;
		color: var(--text-muted);
		font-size: 0.72rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		padding: 0.3rem 0.65rem;
		border-radius: 999px;
		cursor: pointer;
	}

	button.active {
		background: var(--surface-hover);
		color: var(--text);
	}
</style>
