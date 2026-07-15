import type { Generation } from '$lib/studio.svelte';

export type JobStatusSlice = {
	id: 'succeeded' | 'running' | 'queued' | 'failed';
	label: string;
	count: number;
	color: string;
};

export type JobStatusBreakdown = {
	slices: JobStatusSlice[];
	total: number;
};

type JobLabelKey =
	| 'app.metrics.job_succeeded'
	| 'app.metrics.job_running'
	| 'app.metrics.job_queued'
	| 'app.metrics.job_failed';

const SLICE_ORDER: JobStatusSlice['id'][] = ['succeeded', 'running', 'queued', 'failed'];

const SLICE_META: Record<JobStatusSlice['id'], { labelKey: JobLabelKey; color: string }> = {
	succeeded: { labelKey: 'app.metrics.job_succeeded', color: 'var(--status-success)' },
	running: { labelKey: 'app.metrics.job_running', color: 'var(--status-running)' },
	queued: { labelKey: 'app.metrics.job_queued', color: 'var(--status-neutral)' },
	failed: { labelKey: 'app.metrics.job_failed', color: 'var(--status-error)' }
};

export function computeJobStatusBreakdown(
	history: Generation[],
	label: (key: JobLabelKey) => string
): JobStatusBreakdown {
	const counts = { succeeded: 0, running: 0, queued: 0, failed: 0 };
	for (const job of history) {
		if (job.state in counts) {
			counts[job.state as keyof typeof counts] += 1;
		}
	}
	const slices = SLICE_ORDER.map((id) => ({
		id,
		label: label(SLICE_META[id].labelKey),
		count: counts[id],
		color: SLICE_META[id].color
	}));
	const total = Object.values(counts).reduce((sum, count) => sum + count, 0);
	return { slices, total };
}
