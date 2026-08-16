<script lang="ts">
	import BotIcon from '@lucide/svelte/icons/bot';
	import FrameIcon from '@lucide/svelte/icons/frame';
	import PencilIcon from '@lucide/svelte/icons/pencil';
	import ScanLineIcon from '@lucide/svelte/icons/scan-line';
	import SquareTerminalIcon from '@lucide/svelte/icons/square-terminal';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { openService, studio } from '$lib/studio.svelte';

	const services = $derived([
		{
			view: 'generate' as const,
			label: t('app.service.generate'),
			icon: SquareTerminalIcon,
			comingSoon: false
		},
		{
			view: 'image_to_image' as const,
			label: t('app.service.image_to_image'),
			icon: FrameIcon,
			comingSoon: false
		},
		{
			view: 'upscale' as const,
			label: t('app.service.upscale'),
			icon: ScanLineIcon,
			comingSoon: false
		},
		{
			view: 'edit_image' as const,
			label: t('app.service.edit_image'),
			icon: PencilIcon,
			comingSoon: true
		},
		{
			view: 'image_to_text' as const,
			label: t('app.service.image_to_text'),
			icon: BotIcon,
			comingSoon: true
		},
		{
			view: 'realtime_canvas' as const,
			label: t('app.service.realtime_canvas'),
			icon: FrameIcon,
			comingSoon: false
		}
	]);
</script>

<Sidebar.Group>
	<Sidebar.GroupLabel>{t('app.shell.services')}</Sidebar.GroupLabel>
	<Sidebar.Menu>
		{#each services as service (service.view)}
			<Sidebar.MenuItem>
				<Sidebar.MenuButton
					tooltipContent={service.label}
					isActive={studio.shellView === service.view}
				>
					{#snippet child({ props })}
						<button type="button" {...props} onclick={() => openService(service.view)}>
							<service.icon />
							<span>{service.label}</span>
						</button>
					{/snippet}
				</Sidebar.MenuButton>
				{#if service.comingSoon}
					<Sidebar.MenuBadge>{t('app.gen.coming_soon')}</Sidebar.MenuBadge>
				{/if}
			</Sidebar.MenuItem>
		{/each}
		<Sidebar.MenuItem>
			<Sidebar.MenuButton
				tooltipContent={t('app.models.title')}
				isActive={studio.shellView === 'models'}
			>
				{#snippet child({ props })}
					<button type="button" {...props} onclick={() => openService('models')}>
						<BotIcon />
						<span>{t('app.models.title')}</span>
					</button>
				{/snippet}
			</Sidebar.MenuButton>
		</Sidebar.MenuItem>
	</Sidebar.Menu>
</Sidebar.Group>
