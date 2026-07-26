import { collagePreviewList } from '$lib/collage-variants';

export const entries = () => collagePreviewList.map(({ id }) => ({ variant: id }));
