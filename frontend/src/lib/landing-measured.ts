// Public benchmark numbers for the landing page. landing-measured.test.ts
// checks them against static/benchmark/results.json, so a rerun cannot drift.
export const measured = {
	totalJobs: 2160,
	models: 12,
	gpu: 'AMD Radeon RX 7600 XT',
	rows: [
		{ id: 'ssd-1b-lightning', name: 'SSD-1B + Lightning', seconds: 2.2 },
		{ id: 'sdxl-hypersd', name: 'SDXL Hyper-SD', seconds: 3.4 },
		{ id: 'sdxl-fast', name: 'SDXL Fast', seconds: 3.4 },
		{ id: 'ssd-1b', name: 'SSD-1B', seconds: 10.0 },
		{ id: 'sdxl-base', name: 'SDXL', seconds: 14.2 },
		{ id: 'flux2-klein-4b', name: 'FLUX.2 Klein 4B', seconds: 16.1 }
	]
} as const;
