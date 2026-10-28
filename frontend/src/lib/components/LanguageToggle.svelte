<script lang="ts">
	import * as ToggleGroup from '$lib/components/ui/toggle-group';
	import { getLocale, setLocale, locales, t } from '$lib/i18n.svelte';
</script>

<ToggleGroup.Root
	type="single"
	size="sm"
	variant="outline"
	spacing={2}
	value={getLocale()}
	onValueChange={(value) => {
		if (value === 'en' || value === 'es') setLocale(value);
	}}
	aria-label={t('ui.language')}
	class="rounded-full border p-1"
>
	{#each locales as locale (locale)}
		<ToggleGroup.Item
			value={locale}
			lang={locale}
			aria-label={locale === 'en' ? 'English' : 'Espa\u00f1ol'}
			title={locale === 'en' ? 'English' : 'Espa\u00f1ol'}
			class="rounded-full px-3 data-[state=on]:bg-primary data-[state=on]:text-primary-foreground"
		>
			{#if locale === 'en'}
				<svg class="size-4 h-4 w-6" aria-hidden="true" width="24" height="16" viewBox="0 0 60 40">
					<path fill="#012169" d="M0 0h60v40H0z" />
					<path stroke="#fff" stroke-width="8" d="M0 0l60 40M60 0L0 40" />
					<path stroke="#c8102e" stroke-width="4" d="M0 0l60 40M60 0L0 40" />
					<path stroke="#fff" stroke-width="12" d="M30 0v40M0 20h60" />
					<path stroke="#c8102e" stroke-width="7" d="M30 0v40M0 20h60" />
				</svg>
			{:else}
				<svg class="size-4 h-4 w-6" aria-hidden="true" width="24" height="16" viewBox="0 0 60 40">
					<path fill="#aa151b" d="M0 0h60v40H0z" />
					<path fill="#f1bf00" d="M0 10h60v20H0z" />
				</svg>
			{/if}
		</ToggleGroup.Item>
	{/each}
</ToggleGroup.Root>
