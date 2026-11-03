/** @param {HTMLElement} node */
export function pageIndex(node) {
	/** @type {number | undefined} */
	let frame;
	/** @type {Element | null} */
	let activeLink = null;

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
		const nextLink = current?.link ?? null;
		if (nextLink && nextLink !== activeLink) {
			const linkBox = nextLink.getBoundingClientRect();
			const indexBox = node.getBoundingClientRect();
			if (linkBox.top < indexBox.top) node.scrollTop += linkBox.top - indexBox.top;
			else if (linkBox.bottom > indexBox.bottom) node.scrollTop += linkBox.bottom - indexBox.bottom;
		}
		activeLink = nextLink;
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
	document.addEventListener('toggle', schedule, true);
	schedule();
	return {
		destroy() {
			observer.disconnect();
			window.removeEventListener('scroll', schedule);
			window.removeEventListener('resize', schedule);
			document.removeEventListener('toggle', schedule, true);
			if (frame !== undefined) cancelAnimationFrame(frame);
		}
	};
}
