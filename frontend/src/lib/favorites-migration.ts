export type StarOutcome = 'migrated' | 'not-found' | 'failed' | 'invalid';

export function planFavoriteMigration(outcomes: ReadonlyArray<readonly [string, StarOutcome]>): {
	retry: string[];
	missing: number;
} {
	const databaseIsPopulated = outcomes.some(([, outcome]) => outcome === 'migrated');
	const retry: string[] = [];
	let missing = 0;
	for (const [id, outcome] of outcomes) {
		if (outcome === 'migrated') continue;
		if (outcome === 'invalid') missing += 1;
		else if (outcome === 'failed') retry.push(id);
		else if (databaseIsPopulated) missing += 1;
		else retry.push(id);
	}
	return { retry, missing };
}
