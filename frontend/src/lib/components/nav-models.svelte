<script lang="ts">
	import BotIcon from '@lucide/svelte/icons/bot';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { studio } from '$lib/studio.svelte';
</script>

<Sidebar.Group>
	<Sidebar.GroupLabel>{t('app.shell.models')}</Sidebar.GroupLabel>
	<Sidebar.Menu>
		{#each studio.models as model (model.id)}
			<Sidebar.MenuItem>
				<Sidebar.MenuButton
					isActive={studio.modelId === model.id}
					tooltipContent={model.name}
					onclick={() => (studio.modelId = model.id)}
				>
					<BotIcon />
					<span>{model.name}</span>
				</Sidebar.MenuButton>
			</Sidebar.MenuItem>
		{/each}
	</Sidebar.Menu>
</Sidebar.Group>
