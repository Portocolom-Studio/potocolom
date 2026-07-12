<script lang="ts">
	import * as ToggleGroup from '$lib/components/ui/toggle-group';
	import { getLocale, setLocale, locales, t, type Locale } from '$lib/i18n.svelte';
</script>

<ToggleGroup.Root
	type="single"
	size="sm"
	variant="outline"
	value={getLocale()}
	onValueChange={(value) => {
		if (value) setLocale(value as Locale);
	}}
	aria-label={t('ui.language')}
>
	{#each locales as locale (locale)}
		<ToggleGroup.Item value={locale} class="px-3 text-xs font-semibold tracking-wide">
			{locale.toUpperCase()}
		</ToggleGroup.Item>
	{/each}
</ToggleGroup.Root>
