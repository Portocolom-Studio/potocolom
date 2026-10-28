// Filled from the /api/v1/account probe that routes/app/+page.svelte already
// runs to guard studio entry, so the sidebar costs no second request. Null
// means no account to show: AUTH_MODE=none answers 404.
import type { Role } from './account-display';

export type Account = { email: string; role: Role };

export const account = $state<{
	current: Account | null;
	ownerId: string | null;
	localModeConfirmed: boolean;
}>({ current: null, ownerId: null, localModeConfirmed: false });
