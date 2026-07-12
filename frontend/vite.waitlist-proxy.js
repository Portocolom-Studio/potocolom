// @ts-nocheck — Vite dev middleware; Node request types not in frontend tsconfig.
const WAITLIST_TARGET =
	process.env.WAITLIST_PROXY_TARGET ?? 'https://potocolom.leonfuller.com';

async function readRequestBody(req) {
	let data = '';
	for await (const chunk of req) data += chunk;
	return data;
}

function waitlistProxyMiddleware() {
	return async (req, res, next) => {
		if (!req.url?.startsWith('/api/waitlist')) return next();
		if (req.method === 'OPTIONS') {
			res.writeHead(204);
			res.end();
			return;
		}
		if (req.method !== 'POST') {
			res.writeHead(405, { 'content-type': 'application/json' });
			res.end(JSON.stringify({ error: 'method' }));
			return;
		}
		try {
			const body = await readRequestBody(req);
			const upstream = await fetch(`${WAITLIST_TARGET}${req.url}`, {
				method: 'POST',
				headers: {
					'content-type': req.headers['content-type'] ?? 'application/json'
				},
				body
			});
			const payload = await upstream.text();
			res.writeHead(upstream.status, {
				'content-type': upstream.headers.get('content-type') ?? 'application/json'
			});
			res.end(payload);
		} catch {
			res.writeHead(502, { 'content-type': 'application/json' });
			res.end(JSON.stringify({ error: 'proxy' }));
		}
	};
}

/** Dev/preview proxy: the waitlist worker only runs on the live site. */
export function waitlistProxy() {
	const middleware = waitlistProxyMiddleware();
	return {
		name: 'waitlist-proxy',
		configureServer(server) {
			server.middlewares.use(middleware);
		},
		configurePreviewServer(server) {
			server.middlewares.use(middleware);
		}
	};
}
