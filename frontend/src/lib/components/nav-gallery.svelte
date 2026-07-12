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
					<button
						type="button"
						title={generation.params.prompt}
						onclick={() => (studio.selectedId = generation.id)}
					>
						<img
							src={generation.assets[0].url}
							alt={generation.params.prompt ?? generation.id}
							class={'aspect-square w-full rounded-md border object-cover ' +
								(studio.selectedId === generation.id ? 'border-primary' : 'border-border')}
						/>
					</button>
				{/each}
			</div>
		</Sidebar.GroupContent>
	</Sidebar.Group>
{/if}
