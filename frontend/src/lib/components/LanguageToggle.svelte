<script lang="ts">
	import * as ToggleGroup from '$lib/components/ui/toggle-group';
	import { getLocale, setLocale, locales, t, type Locale } from '$lib/i18n.svelte';
</script>

<ToggleGroup.Root
	type="single"
	size="sm"
	variant="outline"
	spacing={2}
	value={getLocale()}
	onValueChange={(value) => {
		if (value) setLocale(value as Locale);
	}}
	aria-label={t('ui.language')}
	class="rounded-full border p-1"
>
	{#each locales as locale (locale)}
		<ToggleGroup.Item
			value={locale}
			class="rounded-full px-4 text-xs font-semibold tracking-wide data-[state=on]:bg-primary data-[state=on]:text-primary-foreground"
		>
			{locale.toUpperCase()}
		</ToggleGroup.Item>
	{/each}
</ToggleGroup.Root>
