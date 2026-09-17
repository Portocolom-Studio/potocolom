// Provenance for /illusions. Every public number on that page must come from here.

export type IllusionGalleryItem = {
	id: string;
	view: string;
	prime: string;
	viewWidth: 512;
	viewHeight: 512;
	primeWidth: 256;
	primeHeight: 256;
	titleKey:
		| 'ill.pair.elephant_swan'
		| 'ill.pair.wolf_raven'
		| 'ill.pair.moose_butterfly'
		| 'ill.pair.giraffe_penguin'
		| 'ill.pair.stag_oak'
		| 'ill.pair.eagle_phoenix';
	uprightKey:
		| 'ill.pair.elephant_swan.upright'
		| 'ill.pair.wolf_raven.upright'
		| 'ill.pair.moose_butterfly.upright'
		| 'ill.pair.giraffe_penguin.upright'
		| 'ill.pair.stag_oak.upright'
		| 'ill.pair.eagle_phoenix.upright';
	invertedKey:
		| 'ill.pair.elephant_swan.inverted'
		| 'ill.pair.wolf_raven.inverted'
		| 'ill.pair.moose_butterfly.inverted'
		| 'ill.pair.giraffe_penguin.inverted'
		| 'ill.pair.stag_oak.inverted'
		| 'ill.pair.eagle_phoenix.inverted';
	pairId: string;
	seed: number;
	style: 'oil' | 'reference_sketch';
	mode: 'joint' | 'indep';
	arm: string;
	score: 5;
	frame: 'none' | 'minor';
	sourcePng: string;
	viewSha256: string;
	primeSha256: string;
};

export const ILLUSION_EXPORT = 'window2-2026-08-clean';
export const ILLUSION_HERO_ID = 'elephant-swan';
export const ILLUSION_PAPER_URL = 'https://diffusionillusions.com';

export const ILLUSION_CODE_DEFAULTS = {
	sdsSteps: 500,
	dreamRounds: 8,
	dreamJoint: false,
	sdsObjective: 'legacy'
} as const;

export const ILLUSION_RESEARCH_RECIPE = {
	sdsSteps: 5000,
	dreamRounds: 1,
	primePx: 256,
	costMultiplier: 3.3
} as const;

export const ILLUSION_CLIP = {
	auc: 0.706,
	bar: 0.75
} as const;

export const ILLUSION_ADAM = {
	withoutPct: 2,
	withPctLow: 44,
	withPctHigh: 60,
	sdsSteps: 250,
	dreamRounds: 4,
	dreamSteps: 150
} as const;

export const ILLUSION_GALLERY: readonly IllusionGalleryItem[] = [
	{
		id: 'elephant-swan',
		view: '/illusions/elephant-swan-view.webp',
		prime: '/illusions/elephant-swan-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.elephant_swan',
		uprightKey: 'ill.pair.elephant_swan.upright',
		invertedKey: 'ill.pair.elephant_swan.inverted',
		pairId: 'elephant_swan',
		seed: 11,
		style: 'oil',
		mode: 'joint',
		arm: 'neg_off_joint',
		score: 5,
		frame: 'none',
		sourcePng: 's5-elephant_swan-seed11-oil-neg_off_joint-final-view1.png',
		viewSha256: '633ddce3f7cf5a470ba5e0d0fca0aceef53119a4dd845c3730d1977f66e05771',
		primeSha256: 'e0597ff229f0671cd7ff5ca92ca7551e7d5a5238b4ecf740fd36f4fa3c899c07'
	},
	{
		id: 'wolf-raven',
		view: '/illusions/wolf-raven-view.webp',
		prime: '/illusions/wolf-raven-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.wolf_raven',
		uprightKey: 'ill.pair.wolf_raven.upright',
		invertedKey: 'ill.pair.wolf_raven.inverted',
		pairId: 'wolf_raven',
		seed: 23,
		style: 'oil',
		mode: 'joint',
		arm: 'neg_on_joint',
		score: 5,
		frame: 'none',
		sourcePng: 's5-wolf_raven-seed23-oil-neg_on_joint-final-view1.png',
		viewSha256: 'e92bd5f588dff82b0443d9b79458a586e68708f79fb2fc6eadc64b7bb41f6be3',
		primeSha256: 'ccad4ab749e69b8c860dfbf7b562d86f4db43bc2964b0f9167faffd2bbfbfc8c'
	},
	{
		id: 'moose-butterfly',
		view: '/illusions/moose-butterfly-view.webp',
		prime: '/illusions/moose-butterfly-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.moose_butterfly',
		uprightKey: 'ill.pair.moose_butterfly.upright',
		invertedKey: 'ill.pair.moose_butterfly.inverted',
		pairId: 'moose_butterfly',
		seed: 23,
		style: 'oil',
		mode: 'joint',
		arm: 'neg_off_joint',
		score: 5,
		frame: 'none',
		sourcePng: 's5-moose_butterfly-seed23-oil-neg_off_joint-final-view1.png',
		viewSha256: 'b6f911c83a282271a399e5d814d90fd6f295f70d7edb0a9acc7d1aa12ee43222',
		primeSha256: '47e43bc04071978d6cb2c5fbb2953a8ce8e34124ba4a0b9e1beec26ca313451c'
	},
	{
		id: 'giraffe-penguin',
		view: '/illusions/giraffe-penguin-view.webp',
		prime: '/illusions/giraffe-penguin-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.giraffe_penguin',
		uprightKey: 'ill.pair.giraffe_penguin.upright',
		invertedKey: 'ill.pair.giraffe_penguin.inverted',
		pairId: 'giraffe_penguin_calibration',
		seed: 23,
		style: 'oil',
		mode: 'indep',
		arm: 'neg_on_indep',
		score: 5,
		frame: 'none',
		sourcePng: 's5-giraffe_penguin_calibration-seed23-oil-neg_on_indep-final-view1.png',
		viewSha256: '30e0c6d5962640f6033f146d0e063949609d5c800fa67be6a8e444b393288dc5',
		primeSha256: '0868f52a6db7001bd33fac697e9e2e133f3e974cb40468c083055c4a55d5cafc'
	},
	{
		id: 'stag-oak',
		view: '/illusions/stag-oak-view.webp',
		prime: '/illusions/stag-oak-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.stag_oak',
		uprightKey: 'ill.pair.stag_oak.upright',
		invertedKey: 'ill.pair.stag_oak.inverted',
		pairId: 'stag_oak',
		seed: 11,
		style: 'reference_sketch',
		mode: 'joint',
		arm: 'joint',
		score: 5,
		frame: 'none',
		sourcePng: 's5-stag_oak-seed11-reference_sketch-joint-final-view1.png',
		viewSha256: '7e7b58063e2dba174b4f943a1f8d08427367cc50712502a14e614407cd19b905',
		primeSha256: 'b9ef98a4bb58d08391cb0367624afc8b17e0de6a9689f119b96418fa254cfff8'
	},
	{
		id: 'eagle-phoenix',
		view: '/illusions/eagle-phoenix-view.webp',
		prime: '/illusions/eagle-phoenix-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.eagle_phoenix',
		uprightKey: 'ill.pair.eagle_phoenix.upright',
		invertedKey: 'ill.pair.eagle_phoenix.inverted',
		pairId: 'eagle_phoenix',
		seed: 23,
		style: 'reference_sketch',
		mode: 'joint',
		arm: 'neg_off_joint',
		score: 5,
		frame: 'minor',
		sourcePng: 's5-eagle_phoenix-seed23-reference_sketch-neg_off_joint-final-view1.png',
		viewSha256: '47d379fbc588fbd314dd85b30b22f07c0fb597fae116639262890a20462b9fc3',
		primeSha256: '36f8062cac00696d16b66cdcffc7d8031df45e9c2624834258323b67ce2c6390'
	}
];

export const ILLUSION_HERO = ILLUSION_GALLERY.find((item) => item.id === ILLUSION_HERO_ID)!;

export const ILLUSION_COPY_VARS = {
	auc: String(ILLUSION_CLIP.auc),
	bar: String(ILLUSION_CLIP.bar),
	codeSds: String(ILLUSION_CODE_DEFAULTS.sdsSteps),
	codeDream: String(ILLUSION_CODE_DEFAULTS.dreamRounds),
	researchSds: String(ILLUSION_RESEARCH_RECIPE.sdsSteps),
	researchDream: String(ILLUSION_RESEARCH_RECIPE.dreamRounds),
	cost: String(ILLUSION_RESEARCH_RECIPE.costMultiplier),
	adamWithout: String(ILLUSION_ADAM.withoutPct),
	adamLow: String(ILLUSION_ADAM.withPctLow),
	adamHigh: String(ILLUSION_ADAM.withPctHigh),
	adamSds: String(ILLUSION_ADAM.sdsSteps),
	adamDream: String(ILLUSION_ADAM.dreamRounds),
	adamDreamSteps: String(ILLUSION_ADAM.dreamSteps),
	export: ILLUSION_EXPORT,
	galleryCount: String(ILLUSION_GALLERY.length),
	jointCount: String(ILLUSION_GALLERY.filter((item) => item.mode === 'joint').length),
	score: String(ILLUSION_GALLERY[0].score)
} as const;

export function fillIllusionCopy(template: string): string {
	return template.replace(/\{(\w+)\}/g, (whole, name: string) => {
		return name in ILLUSION_COPY_VARS
			? ILLUSION_COPY_VARS[name as keyof typeof ILLUSION_COPY_VARS]
			: whole;
	});
}
