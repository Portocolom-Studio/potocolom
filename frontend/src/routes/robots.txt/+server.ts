import { PUBLIC_SITE_MODE } from '$env/static/public';
import type { RequestHandler } from './$types';

export const prerender = true;

export const GET: RequestHandler = () => {
	const body =
		PUBLIC_SITE_MODE === 'landing'
			? [
					'User-agent: *',
					'Allow: /',
					'Sitemap: https://potocolom.leonfuller.com/sitemap.xml',
					''
				].join('\n')
			: ['User-agent: *', 'Disallow: /', ''].join('\n');

	return new Response(body, {
		headers: { 'content-type': 'text/plain; charset=utf-8' }
	});
};
