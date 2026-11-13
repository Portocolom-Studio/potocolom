// Cloud plans and the live drawing hours each one covers. The numbers match
// pricing.t1_b1 to pricing.t3_b2 in i18n/en.json; change both together.
export type PlanKey = 't1' | 't2' | 't3' | 't4';

export type CloudPlan = {
	key: PlanKey;
	price: number | null;
	credits: number | null;
	hours: number | null;
	featured: boolean;
};

export const plans: readonly CloudPlan[] = [
	{ key: 't1', price: 9, credits: 10000, hours: 2.7, featured: false },
	{ key: 't2', price: 24, credits: 35000, hours: 9.5, featured: true },
	{ key: 't3', price: 59, credits: 100000, hours: 27, featured: false },
	{ key: 't4', price: null, credits: null, hours: null, featured: false }
];

// The cheapest plan whose hours cover `hours`. Enterprise (t4) has no fixed
// hours, so anything above the largest fixed plan lands there.
export function planForHours(hours: number): PlanKey {
	const h = Number.isNaN(hours) || hours < 0 ? 0 : hours;
	for (const plan of plans) {
		if (plan.hours !== null && h <= plan.hours) return plan.key;
	}
	return 't4';
}
