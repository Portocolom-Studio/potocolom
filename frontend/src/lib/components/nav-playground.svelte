<script lang="ts">
	import SquareTerminalIcon from '@lucide/svelte/icons/square-terminal';
	import BookOpenIcon from '@lucide/svelte/icons/book-open';
	import Settings2Icon from '@lucide/svelte/icons/settings-2';
	import BotIcon from '@lucide/svelte/icons/bot';
	import HistoryIcon from '@lucide/svelte/icons/history';
	import StarIcon from '@lucide/svelte/icons/star';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import * as Collapsible from '$lib/components/ui/collapsible/index.js';
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
		<Collapsible.Root open>
			{#snippet child({ props })}
				<Sidebar.MenuItem {...props}>
					<Collapsible.Trigger>
						{#snippet child({ props })}
							<Sidebar.MenuButton {...props} tooltipContent={t('app.shell.playground')}>
								<SquareTerminalIcon />
								<span>{t('app.shell.playground')}</span>
								<ChevronRightIcon
									class="ml-auto transition-transform in-data-[state=open]:rotate-90"
								/>
							</Sidebar.MenuButton>
						{/snippet}
					</Collapsible.Trigger>
					<Collapsible.Content>
						<Sidebar.MenuSub>
							<Collapsible.Root open>
								{#snippet child({ props })}
									<Sidebar.MenuSubItem {...props}>
										<Collapsible.Trigger>
											{#snippet child({ props })}
												<Sidebar.MenuSubButton {...props}>
													<BotIcon />
													<span>{t('app.shell.models')}</span>
													<ChevronRightIcon
														class="ml-auto transition-transform in-data-[state=open]:rotate-90"
													/>
												</Sidebar.MenuSubButton>
											{/snippet}
										</Collapsible.Trigger>
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
							<Collapsible.Root open={prompts.length > 0}>
								{#snippet child({ props })}
									<Sidebar.MenuSubItem {...props}>
										<Collapsible.Trigger>
											{#snippet child({ props })}
												<Sidebar.MenuSubButton {...props}>
													<HistoryIcon />
													<span>{t('app.shell.history')}</span>
													<ChevronRightIcon
														class="ml-auto transition-transform in-data-[state=open]:rotate-90"
													/>
												</Sidebar.MenuSubButton>
											{/snippet}
										</Collapsible.Trigger>
										<Collapsible.Content>
											<Sidebar.MenuSub>
												{#each prompts as prompt (prompt)}
													<Sidebar.MenuSubItem>
														<Sidebar.MenuSubButton>
															{#snippet child({ props })}
																<button
																	type="button"
																	{...props}
																	title={prompt}
																	onclick={() => (studio.prompt = prompt)}
																>
																	<span class="truncate">{prompt}</span>
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
							<Sidebar.MenuSubItem>
								<Sidebar.MenuSubButton>
									{#snippet child({ props })}
										<a href={deadLink} {...props}>
											<StarIcon />
											<span>{t('app.shell.starred')}</span>
										</a>
									{/snippet}
								</Sidebar.MenuSubButton>
							</Sidebar.MenuSubItem>
						</Sidebar.MenuSub>
					</Collapsible.Content>
				</Sidebar.MenuItem>
			{/snippet}
		</Collapsible.Root>
		{#each placeholders as section (section.title)}
			<Collapsible.Root>
				{#snippet child({ props })}
					<Sidebar.MenuItem {...props}>
						<Collapsible.Trigger>
							{#snippet child({ props })}
								<Sidebar.MenuButton {...props} tooltipContent={section.title}>
									<section.icon />
									<span>{section.title}</span>
									<ChevronRightIcon
										class="ml-auto transition-transform in-data-[state=open]:rotate-90"
									/>
								</Sidebar.MenuButton>
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
