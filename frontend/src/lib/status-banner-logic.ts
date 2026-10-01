export type BannerKind = 'high_demand' | 'degraded' | 'maintenance';

export type BannerConfig = {
	kind: BannerKind;
	message_key: string | null;
	custom_text: string | null;
} | null;

// A readable value that changes whenever the banner's content does: the
// dismissal sessionStorage holds this, not a boolean, so a dismissed banner
// comes back the moment an administrator changes it rather than staying
// hidden until a session ends that has nothing to do with the change.
export function bannerDismissKey(banner: BannerConfig): string {
	if (banner === null) return '';
	return `${banner.kind}|${banner.message_key ?? ''}|${banner.custom_text ?? ''}`;
}

export function bannerVisible(banner: BannerConfig, dismissed: string | null): boolean {
	if (banner === null) return false;
	return dismissed !== bannerDismissKey(banner);
}

export function bannerDefaultMessageKey(kind: BannerKind): string {
	return `app.banner.${kind}`;
}

// Only destructive and muted exist in the theme today; high_demand is
// informational (the install still works, just busier), degraded and
// maintenance both mean something is actually wrong.
export function bannerTone(kind: BannerKind): 'muted' | 'destructive' {
	return kind === 'high_demand' ? 'muted' : 'destructive';
}

export type BannerMessageChoice = 'default' | 'custom';

export function adminBannerPutBody(
	kind: BannerKind,
	choice: BannerMessageChoice,
	customText: string
): { kind: BannerKind; message_key: string | null; custom_text: string | null } {
	return choice === 'custom'
		? { kind, message_key: null, custom_text: customText.trim() }
		: { kind, message_key: bannerDefaultMessageKey(kind), custom_text: null };
}
