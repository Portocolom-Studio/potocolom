<script lang="ts">
	import ImagesIcon from '@lucide/svelte/icons/images';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { openService, studio } from '$lib/studio.svelte';
</script>

<Sidebar.Group>
	<Sidebar.Menu>
		<Sidebar.MenuItem>
			<Sidebar.MenuButton
				tooltipContent={t('app.images.title')}
				isActive={studio.shellView === 'images'}
			>
				{#snippet child({ props })}
					<button type="button" {...props} onclick={() => openService('images')}>
						<ImagesIcon />
						<span>{t('app.images.title')}</span>
					</button>
				{/snippet}
			</Sidebar.MenuButton>
		</Sidebar.MenuItem>
	</Sidebar.Menu>
</Sidebar.Group>
