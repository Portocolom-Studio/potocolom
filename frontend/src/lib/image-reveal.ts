// Fade a full-size image in over its thumbnail once its bytes arrive. The
// attachment marks the <img> with data-loaded; app.css does the rest
// (.img-reveal, .img-placeholder, .reveal-stack), so reduced motion is one
// media query there. There is no blurhash on the API, so the placeholder is
// the thumbnail the history already carries, blurred.
import type { Attachment } from 'svelte/attachments';

/** Pass the image's src so a new src starts hidden again. A failed load is
 * marked too: the broken image and its alt text must not stay invisible. */
export function revealOnLoad(src: string | null | undefined): Attachment<HTMLImageElement> {
	return (image) => {
		const show = () => {
			image.dataset.loaded = '';
		};
		delete image.dataset.loaded;
		// Cached bytes can finish before this runs; check the attribute too, as
		// the src may not have been swapped in yet.
		if (image.complete && image.naturalWidth > 0 && image.getAttribute('src') === src) show();
		image.addEventListener('load', show);
		image.addEventListener('error', show);
		return () => {
			image.removeEventListener('load', show);
			image.removeEventListener('error', show);
		};
	};
}
