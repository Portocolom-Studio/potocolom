// The first-run setup card in the studio: steps 2 to 4 (the GPU, the first models,
// and the ready summary). Its rules run without a DOM; the card itself is
// components/studio-gpu-card.svelte.
import type en from './i18n/en.json';
import { knownSize, remainingReservation, shouldPoll, sumKnown } from './model-management.ts';

/** Set when the card is finished or dismissed. Its absence is what shows it. */
export const GPU_CARD_DONE_KEY = 'potocolom-gpu-card-done';
// Written and removed to prove storage takes writes. Never read back.
const GPU_CARD_PROBE_KEY = 'potocolom-gpu-card-probe';

type CardStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

/** A message key of the card, checked against en.json at compile time. */
export type CardKey = keyof typeof en;

export type GpuOption = 'this' | 'other';

// Storage is passed as a getter: blocked storage throws on the access itself,
// so the read has to sit inside the try. The same rules as the tour's flag,
// because a card that cannot save its flag would come back on every load.
export function shouldShowGpuCard(canAdminister: boolean, storage: () => CardStorage): boolean {
	if (!canAdminister) return false;
	try {
		const store = storage();
		if (store.getItem(GPU_CARD_DONE_KEY) !== null) return false;
		store.setItem(GPU_CARD_PROBE_KEY, '1');
		store.removeItem(GPU_CARD_PROBE_KEY);
		return true;
	} catch {
		return false;
	}
}

export function markGpuCardDone(storage: () => CardStorage): void {
	try {
		storage().setItem(GPU_CARD_DONE_KEY, '1');
	} catch {
		// Blocked storage: the card cannot show again on this browser anyway.
	}
}

// A null list (still loading, or the request failed) keeps the "this" option:
// the failure must not send the user to a dialog that cannot show their GPUs.
export function defaultGpuOption(workers: string[] | null): GpuOption {
	return workers !== null && workers.length === 0 ? 'other' : 'this';
}

// Only "Other computers" leaves the studio for the fleet tab. The tour starts
// after the card closes by its own first-visit rules, so it never starts on top
// of the Administration view.
export function gpuCardOpensFleet(ending: GpuOption | 'dismissed'): boolean {
	return ending === 'other';
}

// The fields of GET /api/v1/admin/models that the card reads.
export type Device = 'cuda' | 'rocm' | 'cpu';
export type UseClass = 'text_to_image' | 'realtime' | 'upscale';

export type Download = {
	kind: 'install' | 'remove' | 'cancel';
	state: string;
	bytes_done: number;
	bytes_total: number;
	error: string | null;
	op_id: string | null;
};

export type CardModel = {
	id: string;
	name: string;
	capabilities: string[];
	installed: boolean;
	download_bytes: number | null;
	needs_token: boolean;
	download: Download | null;
};

export type AdminWorker = {
	worker_id: string;
	device: Device | null;
	manageable: boolean;
	hardware: {
		gpu_name: string;
		vram_total_bytes: number | null;
		disk_total_bytes: number;
		disk_free_bytes: number;
	} | null;
	disk_free_bytes: number | null;
	suggested: Partial<Record<UseClass, string | null>>;
	models: CardModel[];
};

export const DEVICE_LABELS: Record<Device, string> = { cuda: 'CUDA', rocm: 'ROCm', cpu: 'CPU' };

// The sidebar and the first-run card share these names. Anything else, such as
// a future device string, is shown as the API sent it.
export function deviceLabel(device: string): string {
	if (device === 'cuda' || device === 'rocm' || device === 'cpu') return DEVICE_LABELS[device];
	return device;
}

// The name step 2 shows for a worker, or null while a GPU has not reported
// hardware yet. A CPU worker has no GPU readout to wait for: the device is the
// name, and the card stops asking.
export function workerGpuName(worker: Pick<AdminWorker, 'hardware' | 'device'>): string | null {
	if (worker.hardware !== null) return worker.hardware.gpu_name;
	if (worker.device === 'cpu') return DEVICE_LABELS.cpu;
	return null;
}

// The order of the uses on step 3. A model suggested for two uses is offered once,
// under the first of them.
export const USE_ORDER: readonly UseClass[] = ['text_to_image', 'realtime', 'upscale'];

export type FirstOption = {
	use: UseClass;
	id: string;
	name: string;
	size: number | null;
	installed: boolean;
	download: Download | null;
};

// The worker that step 3 manages: manageable, reporting its hardware, with the most
// video memory. Null when none qualifies, which passes step 3 over.
export function setupWorker(list: AdminWorker[]): AdminWorker | null {
	return list
		.filter((worker) => worker.manageable && worker.hardware !== null)
		.reduce<AdminWorker | null>(
			(best, worker) => (best === null || vramOf(worker) > vramOf(best) ? worker : best),
			null
		);
}

function vramOf(worker: AdminWorker): number {
	return knownSize(worker.hardware?.vram_total_bytes) ?? -1;
}

// The uses the worker suggests, in USE_ORDER, that exist in its model list.
export function firstModels(worker: AdminWorker): FirstOption[] {
	const options: FirstOption[] = [];
	const seen = new Set<string>();
	for (const use of USE_ORDER) {
		const id = worker.suggested[use];
		if (!id || seen.has(id)) continue;
		const model = worker.models.find((item) => item.id === id);
		if (!model) continue;
		seen.add(id);
		options.push({
			use,
			id,
			name: model.name,
			size: knownSize(model.download_bytes),
			installed: model.installed,
			download: model.download
		});
	}
	return options;
}

// Step 3 is passed over when there is no worker to manage, or nothing is left to install.
export function skipsModelStep(worker: AdminWorker | null): boolean {
	return worker === null || firstModels(worker).every((option) => option.installed);
}

// Bytes that the worker's running installs still have to fetch. Null while one of
// them has an unknown size, which keeps the free-space rule closed.
export function reservedBytes(worker: AdminWorker): number | null {
	return sumKnown(
		worker.models.flatMap((model) => {
			const download = model.download;
			if (!download || download.kind !== 'install' || !shouldPoll([download.state])) return [];
			return [
				remainingReservation(download.bytes_total, model.download_bytes, download.bytes_done)
			];
		})
	);
}

export function hasCapability(workers: AdminWorker[], capability: string): boolean {
	return workers.some((worker) =>
		worker.models.some((model) => model.installed && model.capabilities.includes(capability))
	);
}

// Step 4 states only what the list shows: it could not be read (null), no GPU is
// connected, a model that makes images is installed, a download is running, or none
// of these, which sends the user to the Models screen.
export function readyCopy(workers: AdminWorker[] | null): { title: CardKey; body: CardKey } {
	if (workers === null)
		return { title: 'app.gpu_card.unread_title', body: 'app.gpu_card.unread_body' };
	if (workers.length === 0)
		return { title: 'app.gpu_card.no_gpu_title', body: 'app.gpu_card.no_gpu_body' };
	if (hasCapability(workers, 'text_to_image') || hasCapability(workers, 'realtime'))
		return { title: 'app.gpu_card.ready_title', body: 'app.gpu_card.ready_body' };
	const downloading = workers.some((worker) =>
		worker.models.some(
			(model) => model.download?.kind === 'install' && shouldPoll([model.download.state])
		)
	);
	if (downloading)
		return { title: 'app.gpu_card.downloading_title', body: 'app.gpu_card.downloading_body' };
	return { title: 'app.gpu_card.no_model_title', body: 'app.gpu_card.no_model_body' };
}

// The text for a download that stopped, by the worker's error code.
export function failureKey(error: string | null): CardKey {
	switch (error) {
		case 'disk':
			return 'app.gpu_card.failed_disk';
		case 'needs_token':
			return 'app.gpu_card.failed_token';
		case 'cancelled':
			return 'app.gpu_card.failed_cancelled';
		default:
			return 'app.gpu_card.failed_other';
	}
}

export type RowStatus =
	| { kind: 'ready' }
	| { kind: 'progress'; done: number; total: number }
	| { kind: 'waiting' }
	| { kind: 'failed'; key: CardKey };

// What a step 4 row shows for one model.
export function rowStatus(option: Pick<FirstOption, 'installed' | 'size' | 'download'>): RowStatus {
	if (option.installed) return { kind: 'ready' };
	const download = option.download;
	if (download === null) return { kind: 'waiting' };
	if (download.state === 'failed') return { kind: 'failed', key: failureKey(download.error) };
	if (download.state === 'queued' || download.state === 'downloading') {
		const total = download.bytes_total > 0 ? download.bytes_total : (option.size ?? 0);
		if (total > 0) return { kind: 'progress', done: Math.min(download.bytes_done, total), total };
	}
	return { kind: 'waiting' };
}
