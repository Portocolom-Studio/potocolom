<script lang="ts">
	import HistoryIcon from '@lucide/svelte/icons/history';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { studio } from '$lib/studio.svelte';

	// Most recent distinct prompts; clicking one refills the form.
	const prompts = $derived.by(() => {
		const seen = new Set<string>();
		const recent: string[] = [];
		for (const generation of studio.history) {
			const prompt = (generation.params.prompt ?? '').trim();
			if (prompt !== '' && !seen.has(prompt)) {
				seen.add(prompt);
				recent.push(prompt);
			}
			if (recent.length === 10) break;
		}
		return recent;
	});
</script>

{#if prompts.length > 0}
	<Sidebar.Group class="group-data-[collapsible=icon]:hidden">
		<Sidebar.GroupLabel>{t('app.shell.history')}</Sidebar.GroupLabel>
		<Sidebar.Menu>
			{#each prompts as prompt (prompt)}
				<Sidebar.MenuItem>
					<Sidebar.MenuButton title={prompt} onclick={() => (studio.prompt = prompt)}>
						<HistoryIcon />
						<span class="truncate">{prompt}</span>
					</Sidebar.MenuButton>
				</Sidebar.MenuItem>
			{/each}
		</Sidebar.Menu>
	</Sidebar.Group>
{/if}
