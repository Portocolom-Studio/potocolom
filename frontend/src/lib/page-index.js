/** @param {HTMLElement} node */
export function pageIndex(node) {
	/** @type {number | undefined} */
	let frame;

	function update() {
		frame = undefined;
		const links = [...node.querySelectorAll('a[href^="#"]')];
		const targets = links
			.map((link) => ({
				link,
				target: document.getElementById((link.getAttribute('href') ?? '').slice(1))
			}))
			.filter(({ target }) => target && target.getClientRects().length);
		let current = null;
		for (const item of targets) {
			if (item.target && item.target.getBoundingClientRect().top <= 160) current = item;
		}
		if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) {
			current = targets.at(-1) ?? null;
		}
		for (const link of links) {
			if (link === current?.link) link.setAttribute('aria-current', 'location');
			else link.removeAttribute('aria-current');
		}
	}

	function schedule() {
		if (frame === undefined) frame = requestAnimationFrame(update);
	}

	const observer = new MutationObserver(schedule);
	observer.observe(node, { childList: true, subtree: true });
	window.addEventListener('scroll', schedule, { passive: true });
	window.addEventListener('resize', schedule);
	schedule();
	return {
		destroy() {
			observer.disconnect();
			window.removeEventListener('scroll', schedule);
			window.removeEventListener('resize', schedule);
			if (frame !== undefined) cancelAnimationFrame(frame);
		}
	};
}
