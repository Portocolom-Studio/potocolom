import { apiFetch } from './api.ts';

export type ShareAsset = {
	id: string;
	width: number;
	height: number;
	mime: string;
};

export type ShareInfo = {
	asset: ShareAsset;
	prompt: string | null;
	model: string | null;
	url: string;
};

export class ShareGoneError extends Error {}

export async function resolveShare(token: string): Promise<ShareInfo> {
	const response = await apiFetch('/api/v1/shared', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ token })
	});
	if (response.status === 404) throw new ShareGoneError();
	if (!response.ok) throw new Error(`share resolve failed: ${response.status}`);
	return (await response.json()) as ShareInfo;
}

export function shareDownloadName(assetId: string, mime: string): string {
	const extension = mime === 'image/png' ? 'png' : mime === 'image/webp' ? 'webp' : 'bin';
	return `potocolom-${assetId}.${extension}`;
}

export async function downloadSharedPicture(
	token: string,
	doc: Document = document
): Promise<void> {
	// The address in the page lasts a minute, so a click later still needs a
	// fresh one; resolve the token again and point the download at that.
	const info = await resolveShare(token);
	const anchor = doc.createElement('a');
	anchor.href = info.url;
	anchor.download = shareDownloadName(info.asset.id, info.asset.mime);
	doc.body.appendChild(anchor);
	anchor.click();
	anchor.remove();
}
