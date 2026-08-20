const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STARRED_STATUS = 204;

export async function runFavoriteMigration(
	stored: readonly string[],
	star: (id: string) => Promise<number | null>
): Promise<{ retry: string[]; missing: number }> {
	const retry: string[] = [];
	let missing = 0;
	for (const id of stored) {
		if (!UUID_PATTERN.test(id)) {
			missing += 1;
			continue;
		}
		if ((await star(id)) !== STARRED_STATUS) retry.push(id);
	}
	return { retry, missing };
}
