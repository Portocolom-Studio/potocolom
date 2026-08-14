<script lang="ts">
	import { Label } from '$lib/components/ui/label';
	import { Slider } from '$lib/components/ui/slider';

	let {
		id,
		label,
		norm = $bindable(0),
		steps = 100,
		minLabel,
		maxLabel,
		valueLabel,
		disabled = false
	}: {
		id: string;
		label: string;
		norm?: number;
		steps?: number;
		minLabel: string;
		maxLabel: string;
		valueLabel: string;
		disabled?: boolean;
	} = $props();

	// The track carries one notch per parameter step, so an arrow key moves the
	// parameter by one of its own steps rather than by one percent of a fixed
	// track (issue #250). The bar still spans the same width whatever the model
	// declares, which is what the old fixed 0-100 track was protecting.
	const notches = $derived(Math.max(1, Math.round(steps)));
	const sliderPosition = $derived(Math.round(Math.min(1, Math.max(0, norm)) * notches));

	function onValueChange(value: number): void {
		norm = value / notches;
	}
</script>

<div class="flex flex-col gap-2">
	<div class="flex items-center justify-between gap-2">
		<Label for={id}>{label}</Label>
		<span class="text-muted-foreground text-xs tabular-nums">{valueLabel}</span>
	</div>
	<Slider
		{id}
		type="single"
		min={0}
		max={notches}
		step={1}
		{disabled}
		value={sliderPosition}
		{onValueChange}
		class="w-full"
	/>
	<div class="text-muted-foreground flex justify-between text-xs tabular-nums">
		<span>{minLabel}</span>
		<span>{maxLabel}</span>
	</div>
</div>
