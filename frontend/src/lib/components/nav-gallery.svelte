<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { studio } from '$lib/studio.svelte';

	const finished = $derived(studio.history.filter((g) => g.assets.length > 0));
</script>

{#if finished.length > 0}
	<Sidebar.Group class="group-data-[collapsible=icon]:hidden">
		<Sidebar.GroupLabel>{t('app.shell.gallery')}</Sidebar.GroupLabel>
		<Sidebar.GroupContent>
			<div class="grid grid-cols-3 gap-1.5 px-2">
				{#each finished as generation (generation.id)}
					<a href={generation.assets[0].url} target="_blank" rel="noopener">
						<img
							src={generation.assets[0].url}
							alt={generation.params.prompt ?? generation.id}
							title={generation.params.prompt}
							class="border-border aspect-square w-full rounded-md border object-cover"
						/>
					</a>
				{/each}
			</div>
		</Sidebar.GroupContent>
	</Sidebar.Group>
{/if}
