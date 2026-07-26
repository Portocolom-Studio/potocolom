import { PUBLIC_SITE_MODE } from '$env/static/public';
import type { RequestHandler } from './$types';

export const prerender = true;

const siteUrl = 'https://potocolom.leonfuller.com';
const marketingPaths = ['/', '/app', '/benchmark', '/whitepaper'];

export const GET: RequestHandler = () => {
	const urls =
		PUBLIC_SITE_MODE === 'landing'
			? marketingPaths.map((path) => `  <url><loc>${siteUrl}${path}</loc></url>`).join('\n')
			: '';
	const body = [
		'<?xml version="1.0" encoding="UTF-8"?>',
		'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
		urls,
		'</urlset>',
		''
	].join('\n');

	return new Response(body, {
		headers: { 'content-type': 'application/xml; charset=utf-8' }
	});
};
