// Toasts for the studio's quick actions: an Undo offer after a reversible
// removal, and an error when an optimistic change had to be rolled back.
// Components only: svelte-sonner ships .svelte files the unit test loader
// does not compile, so the state modules stay free of it.
import { toast } from 'svelte-sonner';
import { t } from '$lib/i18n.svelte';
import { isStarred } from '$lib/studio.svelte';

/** Offer to take back what just happened. Returns the toast id, so the
 * caller can withdraw the offer once undoing would no longer be right. */
export function offerUndo(message: string, undo: () => void): string | number {
	return toast(message, { action: { label: t('app.toast.undo'), onClick: undo } });
}

/** Star or unstar through `toggle`, which updates the view at once and rolls
 * it back when the API refuses. An unstar offers Undo straight away; a
 * rollback replaces that offer with an error. */
export async function toggleStarWithUndo(
	id: string,
	toggle: () => Promise<boolean>
): Promise<boolean> {
	const unstarring = isStarred(id);
	const offer = unstarring
		? offerUndo(t('app.toast.unstarred'), () => {
				// Re-star only while it is still unstarred: a second click on the
				// star may have done it already.
				if (!isStarred(id)) void toggle().then(reportRollback);
			})
		: null;
	const succeeded = await toggle();
	if (!succeeded) {
		if (offer !== null) toast.dismiss(offer);
		reportRollback(false);
	}
	return succeeded;
}

function reportRollback(succeeded: boolean): void {
	if (!succeeded) toast.error(t('app.gen.favorite_save_failed'));
}
