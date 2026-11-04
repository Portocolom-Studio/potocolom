import { error } from '@sveltejs/kit';
import { PUBLIC_SITE_MODE } from '$env/static/public';

// Public-site pages exist in the landing build only. Elsewhere they are not
// prerendered, so the host answers its fallback 404 page, and a client-side
// visit gets the same not-found page from this load.
const landing = PUBLIC_SITE_MODE === 'landing';

export const prerender = landing;

export const load = () => {
	if (!landing) error(404, 'Not found');
};
