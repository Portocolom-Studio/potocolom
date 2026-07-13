<script lang="ts">
	import SquareTerminalIcon from '@lucide/svelte/icons/square-terminal';
	import BookOpenIcon from '@lucide/svelte/icons/book-open';
	import Settings2Icon from '@lucide/svelte/icons/settings-2';
	import BotIcon from '@lucide/svelte/icons/bot';
	import HistoryIcon from '@lucide/svelte/icons/history';
	import StarIcon from '@lucide/svelte/icons/star';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import { t } from '$lib/i18n.svelte';
	import { cn } from '$lib/utils.js';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import * as Collapsible from '$lib/components/ui/collapsible/index.js';
	import { studio, starredGenerations } from '$lib/studio.svelte';

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

	const starred = $derived(starredGenerations());

	// The placeholder sections keep their original shape (dead links until
	// their issues land).
	const deadLink = '#';
	const placeholders = [
		{
			title: t('app.shell.documentation'),
			icon: BookOpenIcon,
			items: [
				t('app.shell.introduction'),
				t('app.shell.get_started'),
				t('app.shell.tutorials'),
				t('app.shell.changelog')
			]
		},
		{
			title: t('app.shell.settings'),
			icon: Settings2Icon,
			items: [
				t('app.shell.general'),
				t('app.shell.team'),
				t('app.shell.billing'),
				t('app.shell.limits')
			]
		}
	];
</script>

<Sidebar.Group>
	<Sidebar.GroupLabel>{t('app.shell.platform')}</Sidebar.GroupLabel>
	<Sidebar.Menu>
		<Collapsible.Root open class="group/collapsible">
			{#snippet child({ props })}
				<Sidebar.MenuItem {...props}>
					<Sidebar.MenuButton tooltipContent={t('app.shell.playground')}>
						<SquareTerminalIcon />
						<span>{t('app.shell.playground')}</span>
					</Sidebar.MenuButton>
					<Collapsible.Trigger>
						{#snippet child({ props: triggerProps })}
							<Sidebar.MenuAction {...triggerProps}>
								<ChevronRightIcon
									class="transition-transform group-data-[state=open]/collapsible:rotate-90"
								/>
							</Sidebar.MenuAction>
						{/snippet}
					</Collapsible.Trigger>
					<Collapsible.Content>
						<Sidebar.MenuSub>
							<Collapsible.Root open class="group/collapsible">
								{#snippet child({ props })}
									<Sidebar.MenuSubItem {...props}>
										<div class="flex w-full items-center">
											<Sidebar.MenuSubButton class="min-w-0 flex-1">
												{#snippet child({ props: buttonProps })}
													<div {...buttonProps}>
														<BotIcon />
														<span>{t('app.shell.models')}</span>
													</div>
												{/snippet}
											</Sidebar.MenuSubButton>
											<Collapsible.Trigger>
												{#snippet child({ props: triggerProps })}
													<button
														type="button"
														class="text-sidebar-foreground hover:text-foreground flex size-6 shrink-0 items-center justify-center rounded-md outline-hidden focus-visible:ring-2"
														{...triggerProps}
													>
														<ChevronRightIcon
															class="size-4 transition-transform group-data-[state=open]/collapsible:rotate-90"
														/>
													</button>
												{/snippet}
											</Collapsible.Trigger>
										</div>
										<Collapsible.Content>
											<Sidebar.MenuSub>
												{#each studio.models as model (model.id)}
													<Sidebar.MenuSubItem>
														<Sidebar.MenuSubButton isActive={studio.modelId === model.id}>
															{#snippet child({ props })}
																<button
																	type="button"
																	{...props}
																	title={model.name}
																	onclick={() => (studio.modelId = model.id)}
																>
																	<span class="truncate">{model.name}</span>
																</button>
															{/snippet}
														</Sidebar.MenuSubButton>
													</Sidebar.MenuSubItem>
												{/each}
											</Sidebar.MenuSub>
										</Collapsible.Content>
									</Sidebar.MenuSubItem>
								{/snippet}
							</Collapsible.Root>
							<Collapsible.Root open class="group/collapsible">
								{#snippet child({ props })}
									<Sidebar.MenuSubItem {...props}>
										<div class="flex w-full items-center">
											<Sidebar.MenuSubButton class="min-w-0 flex-1">
												{#snippet child({ props: buttonProps })}
													<div {...buttonProps}>
														<HistoryIcon />
														<span>{t('app.shell.history')}</span>
													</div>
												{/snippet}
											</Sidebar.MenuSubButton>
											<Collapsible.Trigger>
												{#snippet child({ props: triggerProps })}
													<button
														type="button"
														class="text-sidebar-foreground hover:text-foreground flex size-6 shrink-0 items-center justify-center rounded-md outline-hidden focus-visible:ring-2"
														{...triggerProps}
													>
														<ChevronRightIcon
															class="size-4 transition-transform group-data-[state=open]/collapsible:rotate-90"
														/>
													</button>
												{/snippet}
											</Collapsible.Trigger>
										</div>
										<Collapsible.Content>
											<Sidebar.MenuSub>
												{#each prompts as prompt (prompt)}
													<Sidebar.MenuSubItem class="min-w-0">
														<Sidebar.MenuSubButton class="min-w-0">
															{#snippet child({ props })}
																<button
																	type="button"
																	{...props}
																	class={cn(
																		'min-w-0 w-full max-w-full',
																		props.class as string | undefined
																	)}
																	title={prompt}
																	onclick={() => (studio.prompt = prompt)}
																>
																	<span class="block min-w-0 truncate">{prompt}</span>
																</button>
															{/snippet}
														</Sidebar.MenuSubButton>
													</Sidebar.MenuSubItem>
												{/each}
											</Sidebar.MenuSub>
										</Collapsible.Content>
									</Sidebar.MenuSubItem>
								{/snippet}
							</Collapsible.Root>
							<Collapsible.Root open class="group/collapsible">
								{#snippet child({ props })}
									<Sidebar.MenuSubItem {...props}>
										<div class="flex w-full items-center">
											<Sidebar.MenuSubButton class="min-w-0 flex-1">
												{#snippet child({ props: buttonProps })}
													<div {...buttonProps}>
														<StarIcon />
														<span>{t('app.shell.starred')}</span>
													</div>
												{/snippet}
											</Sidebar.MenuSubButton>
											<Collapsible.Trigger>
												{#snippet child({ props: triggerProps })}
													<button
														type="button"
														class="text-sidebar-foreground hover:text-foreground flex size-6 shrink-0 items-center justify-center rounded-md outline-hidden focus-visible:ring-2"
														{...triggerProps}
													>
														<ChevronRightIcon
															class="size-4 transition-transform group-data-[state=open]/collapsible:rotate-90"
														/>
													</button>
												{/snippet}
											</Collapsible.Trigger>
										</div>
										<Collapsible.Content>
											<div class="grid grid-cols-3 gap-1.5 px-2 py-1">
												{#each starred as generation (generation.id)}
													<button
														type="button"
														title={generation.params.prompt}
														onclick={() => (studio.selectedId = generation.id)}
													>
														<img
															src={generation.assets[0].url}
															alt={generation.params.prompt ?? generation.id}
															class={'aspect-square w-full rounded-md border object-cover ' +
																(studio.selectedId === generation.id
																	? 'border-primary'
																	: 'border-border')}
														/>
													</button>
												{/each}
											</div>
										</Collapsible.Content>
									</Sidebar.MenuSubItem>
								{/snippet}
							</Collapsible.Root>
						</Sidebar.MenuSub>
					</Collapsible.Content>
				</Sidebar.MenuItem>
			{/snippet}
		</Collapsible.Root>
		{#each placeholders as section (section.title)}
			<Collapsible.Root class="group/collapsible">
				{#snippet child({ props })}
					<Sidebar.MenuItem {...props}>
						<Sidebar.MenuButton tooltipContent={section.title}>
							<section.icon />
							<span>{section.title}</span>
						</Sidebar.MenuButton>
						<Collapsible.Trigger>
							{#snippet child({ props: triggerProps })}
								<Sidebar.MenuAction {...triggerProps}>
									<ChevronRightIcon
										class="transition-transform group-data-[state=open]/collapsible:rotate-90"
									/>
								</Sidebar.MenuAction>
							{/snippet}
						</Collapsible.Trigger>
						<Collapsible.Content>
							<Sidebar.MenuSub>
								{#each section.items as title (title)}
									<Sidebar.MenuSubItem>
										<Sidebar.MenuSubButton>
											{#snippet child({ props })}
												<a href={deadLink} {...props}>
													<span>{title}</span>
												</a>
											{/snippet}
										</Sidebar.MenuSubButton>
									</Sidebar.MenuSubItem>
								{/each}
							</Sidebar.MenuSub>
						</Collapsible.Content>
					</Sidebar.MenuItem>
				{/snippet}
			</Collapsible.Root>
		{/each}
	</Sidebar.Menu>
</Sidebar.Group>
