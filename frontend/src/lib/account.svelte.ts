// The signed-in account, filled once from the /api/v1/account probe that
// routes/app/+page.svelte already runs to guard studio entry. Stays null
// until that probe answers 200 (a 404 with AUTH_MODE=none, a non-200 status,
// or no answer at all all leave it null), so the sidebar can tell "no
// account" from "not loaded yet" and render no identity rather than a fake one.
import type { Role } from './account-display';

export type Account = { email: string; role: Role };

export const account = $state<{ current: Account | null }>({ current: null });
