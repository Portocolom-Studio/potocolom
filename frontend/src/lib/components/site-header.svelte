<script lang="ts">
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import SidebarIcon from '@lucide/svelte/icons/sidebar';
	import SearchForm from './search-form.svelte';
	import * as Breadcrumb from '$lib/components/ui/breadcrumb/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Separator } from '$lib/components/ui/separator/index.js';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { t } from '$lib/i18n.svelte';

	const sidebar = Sidebar.useSidebar();
</script>

<header class="bg-background sticky top-0 z-50 flex w-full items-center border-b">
	<div class="flex h-(--header-height) w-full items-center gap-2 px-4">
		<Button class="size-8" variant="ghost" size="icon" onclick={sidebar.toggle}>
			<SidebarIcon />
		</Button>
		<Separator orientation="vertical" class="me-2 h-4" />
		<Breadcrumb.Root class="hidden sm:block">
			<Breadcrumb.List>
				<Breadcrumb.Item>
					<Breadcrumb.Link href="##">{t('app.title')}</Breadcrumb.Link>
				</Breadcrumb.Item>
				<Breadcrumb.Separator />
				<Breadcrumb.Item>
					<Breadcrumb.Page>{t('app.tab_draw')}</Breadcrumb.Page>
				</Breadcrumb.Item>
			</Breadcrumb.List>
		</Breadcrumb.Root>
		<div class="flex w-full items-center gap-3 sm:ms-auto sm:w-auto">
			<SearchForm class="hidden w-full sm:block sm:w-auto" />
			<LanguageToggle />
		</div>
	</div>
</header>
