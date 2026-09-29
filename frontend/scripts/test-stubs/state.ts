// $app/state stand-in: the studio route does not read it, the stub only has to
// give an importer something.

export const page = { url: new URL('https://studio.test/app') };

export const navigating = null;

export const updated = { check: () => false };
