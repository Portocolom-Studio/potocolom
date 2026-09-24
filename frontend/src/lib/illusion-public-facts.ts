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
		| 'ill.pair.eagle_phoenix'
		| 'ill.pair.deer_turtle'
		| 'ill.pair.bear_salmon'
		| 'ill.pair.dog_sloth'
		| 'ill.pair.heron_swan'
		| 'ill.pair.horse_wave'
		| 'ill.pair.kingfisher_wisteria'
		| 'ill.pair.lighthouse_goblet'
		| 'ill.pair.lightningtree_rootweb'
		| 'ill.pair.octopus_camel'
		| 'ill.pair.oryx_agaveflower'
		| 'ill.pair.penguin_bat'
		| 'ill.pair.rabbit_fox'
		| 'ill.pair.scythe_crescentdune'
		| 'ill.pair.squirrel_pelican';
	uprightKey:
		| 'ill.pair.elephant_swan.upright'
		| 'ill.pair.wolf_raven.upright'
		| 'ill.pair.moose_butterfly.upright'
		| 'ill.pair.giraffe_penguin.upright'
		| 'ill.pair.stag_oak.upright'
		| 'ill.pair.eagle_phoenix.upright'
		| 'ill.pair.deer_turtle.upright'
		| 'ill.pair.bear_salmon.upright'
		| 'ill.pair.dog_sloth.upright'
		| 'ill.pair.heron_swan.upright'
		| 'ill.pair.horse_wave.upright'
		| 'ill.pair.kingfisher_wisteria.upright'
		| 'ill.pair.lighthouse_goblet.upright'
		| 'ill.pair.lightningtree_rootweb.upright'
		| 'ill.pair.octopus_camel.upright'
		| 'ill.pair.oryx_agaveflower.upright'
		| 'ill.pair.penguin_bat.upright'
		| 'ill.pair.rabbit_fox.upright'
		| 'ill.pair.scythe_crescentdune.upright'
		| 'ill.pair.squirrel_pelican.upright';
	invertedKey:
		| 'ill.pair.elephant_swan.inverted'
		| 'ill.pair.wolf_raven.inverted'
		| 'ill.pair.moose_butterfly.inverted'
		| 'ill.pair.giraffe_penguin.inverted'
		| 'ill.pair.stag_oak.inverted'
		| 'ill.pair.eagle_phoenix.inverted'
		| 'ill.pair.deer_turtle.inverted'
		| 'ill.pair.bear_salmon.inverted'
		| 'ill.pair.dog_sloth.inverted'
		| 'ill.pair.heron_swan.inverted'
		| 'ill.pair.horse_wave.inverted'
		| 'ill.pair.kingfisher_wisteria.inverted'
		| 'ill.pair.lighthouse_goblet.inverted'
		| 'ill.pair.lightningtree_rootweb.inverted'
		| 'ill.pair.octopus_camel.inverted'
		| 'ill.pair.oryx_agaveflower.inverted'
		| 'ill.pair.penguin_bat.inverted'
		| 'ill.pair.rabbit_fox.inverted'
		| 'ill.pair.scythe_crescentdune.inverted'
		| 'ill.pair.squirrel_pelican.inverted';
	pairId: string;
	seed: number;
	style: 'oil' | 'reference_sketch';
	mode: 'joint' | 'indep';
	arm: string;
	score: 4 | 5;
	frame: 'none' | 'minor';
	sourcePng: string;
	viewSha256: string;
	primeSha256: string;
};

export type IllusionCandidateItem = {
	id: string;
	view: string;
	prime: string;
	viewWidth: number;
	viewHeight: number;
	primeWidth: number;
	primeHeight: number;
	titleKey: IllusionGalleryItem['titleKey'];
	uprightKey: IllusionGalleryItem['uprightKey'];
	invertedKey: IllusionGalleryItem['invertedKey'];
	pairId: string;
	seed: number;
	style: string;
	mode: 'joint' | 'indep';
	arm: string;
	score: 4 | 5;
	frame: 'none' | 'minor' | 'unrated';
	exportTag: string;
	sourcePng: string;
	viewSha256: string;
	primeSha256: string;
};

export const ILLUSION_EXPORT = 'window2-2026-08-clean';
// The review kept this many cells in the export; the gallery shows a hand-picked subset.
export const ILLUSION_EXPORT_KEEPERS = 26;
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
		id: 'elephant-swan-neg-on',
		view: '/illusions/elephant-swan-neg-on-view.webp',
		prime: '/illusions/elephant-swan-neg-on-prime.webp',
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
		arm: 'neg_on_joint',
		score: 5,
		frame: 'none',
		sourcePng: 's5-elephant_swan-seed11-oil-neg_on_joint-final-view1.png',
		viewSha256: '9dd7d3b356313f5e122618b43dc3071f2ad68602cf03c4a21e6ae93f7bf4000b',
		primeSha256: 'd0f366247d6533103b85f9e2b2519e6a31a1181f72e0344c8e9e7539fa34a7b8'
	},
	{
		id: 'elephant-swan-sketch',
		view: '/illusions/elephant-swan-sketch-view.webp',
		prime: '/illusions/elephant-swan-sketch-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.elephant_swan',
		uprightKey: 'ill.pair.elephant_swan.upright',
		invertedKey: 'ill.pair.elephant_swan.inverted',
		pairId: 'elephant_swan',
		seed: 11,
		style: 'reference_sketch',
		mode: 'joint',
		arm: 'neg_off_joint',
		score: 5,
		frame: 'minor',
		sourcePng: 's5-elephant_swan-seed11-reference_sketch-neg_off_joint-final-view1.png',
		viewSha256: '53547fa32baf68b9af079c4c107791980bcbf26a55467591e2736c70c73e9118',
		primeSha256: 'ddd59244e9de97de4215ecb33c41c226da01fbcc32d234af20f4ef8f861fe5cf'
	},
	{
		id: 'elephant-swan-seed37',
		view: '/illusions/elephant-swan-seed37-view.webp',
		prime: '/illusions/elephant-swan-seed37-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.elephant_swan',
		uprightKey: 'ill.pair.elephant_swan.upright',
		invertedKey: 'ill.pair.elephant_swan.inverted',
		pairId: 'elephant_swan',
		seed: 37,
		style: 'oil',
		mode: 'joint',
		arm: 'neg_off_joint',
		score: 4,
		frame: 'none',
		sourcePng: 's4-elephant_swan-seed37-oil-neg_off_joint-final-view1.png',
		viewSha256: 'cb8e0a1e7b7ecfab36ef3ca413ccd524e5d30d576111b6ed3196a84ac0d81b92',
		primeSha256: '432a3553ce826d3b7b9cf92483863063360e7defdae2b3c5e33291aa57b1ea31'
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
		id: 'wolf-raven-seed11',
		view: '/illusions/wolf-raven-seed11-view.webp',
		prime: '/illusions/wolf-raven-seed11-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.wolf_raven',
		uprightKey: 'ill.pair.wolf_raven.upright',
		invertedKey: 'ill.pair.wolf_raven.inverted',
		pairId: 'wolf_raven',
		seed: 11,
		style: 'reference_sketch',
		mode: 'indep',
		arm: 'neg_off_indep',
		score: 5,
		frame: 'minor',
		sourcePng: 's5-wolf_raven-seed11-reference_sketch-neg_off_indep-final-view1.png',
		viewSha256: '84769ae4782475246286203b1a27452dade60a7aabe628eafc8bed0c87807af1',
		primeSha256: 'e7502149e559fc7a40444f576d0495a1a333896e5586c785066a97c6991f464c'
	},
	{
		id: 'wolf-raven-seed53',
		view: '/illusions/wolf-raven-seed53-view.webp',
		prime: '/illusions/wolf-raven-seed53-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.wolf_raven',
		uprightKey: 'ill.pair.wolf_raven.upright',
		invertedKey: 'ill.pair.wolf_raven.inverted',
		pairId: 'wolf_raven',
		seed: 53,
		style: 'reference_sketch',
		mode: 'indep',
		arm: 'indep',
		score: 4,
		frame: 'none',
		sourcePng: 's4-wolf_raven-seed53-reference_sketch-indep-final-view1.png',
		viewSha256: '15dbb4756827f56def8120b9798ce23277767991cd5bad13e9c0622b80eb9464',
		primeSha256: '9a0d5826d87df177a90962ac02c692c88568cd3569613484e2765601154a51c5'
	},
	{
		id: 'moose-butterfly-neg-on',
		view: '/illusions/moose-butterfly-neg-on-view.webp',
		prime: '/illusions/moose-butterfly-neg-on-prime.webp',
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
		arm: 'neg_on_joint',
		score: 5,
		frame: 'none',
		sourcePng: 's5-moose_butterfly-seed23-oil-neg_on_joint-final-view1.png',
		viewSha256: '034096c62e3c594ceb3a36148c930c9eba5adff2b87a30a81292abe38e3e2724',
		primeSha256: '42721b1e56384b7fb656576c10e3cd01694f6aec8d78f438e73d0b8bfb5c76c9'
	},
	{
		id: 'moose-butterfly-seed11',
		view: '/illusions/moose-butterfly-seed11-view.webp',
		prime: '/illusions/moose-butterfly-seed11-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.moose_butterfly',
		uprightKey: 'ill.pair.moose_butterfly.upright',
		invertedKey: 'ill.pair.moose_butterfly.inverted',
		pairId: 'moose_butterfly',
		seed: 11,
		style: 'reference_sketch',
		mode: 'joint',
		arm: 'neg_off_joint',
		score: 5,
		frame: 'minor',
		sourcePng: 's5-moose_butterfly-seed11-reference_sketch-neg_off_joint-final-view1.png',
		viewSha256: '9ab68f8fdef93e66645323de1776545d81e2cfab0e647a09305093726bbc5d1b',
		primeSha256: '060f74c6a0b5fc17a9c2b9d9277ab2eca19ab55cfcc70263f8a9cbc0422cf40f'
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
		id: 'giraffe-penguin-seed37',
		view: '/illusions/giraffe-penguin-seed37-view.webp',
		prime: '/illusions/giraffe-penguin-seed37-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.giraffe_penguin',
		uprightKey: 'ill.pair.giraffe_penguin.upright',
		invertedKey: 'ill.pair.giraffe_penguin.inverted',
		pairId: 'giraffe_penguin_calibration',
		seed: 37,
		style: 'reference_sketch',
		mode: 'joint',
		arm: 'neg_off_joint',
		score: 5,
		frame: 'minor',
		sourcePng:
			's5-giraffe_penguin_calibration-seed37-reference_sketch-neg_off_joint-final-view1.png',
		viewSha256: '6f26f1bccb5ac5662523cb3e73e5daa0cdcf1fe0d1dc72b83511f217372af0e1',
		primeSha256: '32d9c272c0f465d229c60cc7fc9ef6a3946880daf0dedc409a7b56661abca50c'
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
	},
	{
		id: 'deer-turtle-neg-off',
		view: '/illusions/deer-turtle-neg-off-view.webp',
		prime: '/illusions/deer-turtle-neg-off-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.deer_turtle',
		uprightKey: 'ill.pair.deer_turtle.upright',
		invertedKey: 'ill.pair.deer_turtle.inverted',
		pairId: 'deer_turtle',
		seed: 11,
		style: 'reference_sketch',
		mode: 'joint',
		arm: 'neg_off_joint',
		score: 4,
		frame: 'none',
		sourcePng: 's4-deer_turtle-seed11-reference_sketch-neg_off_joint-final-view1.png',
		viewSha256: 'd0db948be34c54ebdb0e8d2c2ee2ed855357fd7fd9caa69ee20a5992d61d19b2',
		primeSha256: '1ea4f3285e7b5b78048a22608a2b8884a083637c552c67f9c863dc999e1af3ef'
	}
];

export const ILLUSION_CANDIDATES: readonly IllusionCandidateItem[] = [
	{
		id: 'c-bear-salmon-37-joint-dreamd1',
		view: '/illusions/c-bear-salmon-37-joint-dreamd1-view.webp',
		prime: '/illusions/c-bear-salmon-37-joint-dreamd1-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.bear_salmon',
		uprightKey: 'ill.pair.bear_salmon.upright',
		invertedKey: 'ill.pair.bear_salmon.inverted',
		pairId: 'bear_salmon',
		seed: 37,
		style: '',
		mode: 'joint',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-bear_salmon-seed37-joint-dream_d1-view1.png',
		viewSha256: 'fe42e705acb13fe32787b5780e40b5ee6bfbdeec241b45ba547cc4336fbbb2c5',
		primeSha256: '46a47f16409b8697f2a0f04d09f0f9e2a5683d49ca35f89a895572bab6cdbf97'
	},
	{
		id: 'c-deer-turtle-11-joint-sdsend',
		view: '/illusions/c-deer-turtle-11-joint-sdsend-view.webp',
		prime: '/illusions/c-deer-turtle-11-joint-sdsend-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.deer_turtle',
		uprightKey: 'ill.pair.deer_turtle.upright',
		invertedKey: 'ill.pair.deer_turtle.inverted',
		pairId: 'deer_turtle',
		seed: 11,
		style: '',
		mode: 'joint',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-deer_turtle-seed11-joint-sds_end-view1.png',
		viewSha256: 'd9004e6e89f96f481b544b5b5bb2a6eb27ebf1ea99c864ebd54fbb3ae64065bc',
		primeSha256: 'd8447e397b64eb63346095baae6c6b0393e2fb1c8e50370b6ca23661893c2651'
	},
	{
		id: 'c-dog-sloth-23-indep',
		view: '/illusions/c-dog-sloth-23-indep-view.webp',
		prime: '/illusions/c-dog-sloth-23-indep-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.dog_sloth',
		uprightKey: 'ill.pair.dog_sloth.upright',
		invertedKey: 'ill.pair.dog_sloth.inverted',
		pairId: 'dog_sloth',
		seed: 23,
		style: '',
		mode: 'indep',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-dog_sloth-seed23-indep-final-view1.png',
		viewSha256: 'e9eec846029294c3d567b21cc38e5e4ce08f57379f7e4214a0842d5cfe6d04a7',
		primeSha256: '40ed6f547105fb92bd7e53d3b504df139ccf49a394dcf0c50292e3671ed45b6f'
	},
	{
		id: 'c-eagle-phoenix-11-indep',
		view: '/illusions/c-eagle-phoenix-11-indep-view.webp',
		prime: '/illusions/c-eagle-phoenix-11-indep-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.eagle_phoenix',
		uprightKey: 'ill.pair.eagle_phoenix.upright',
		invertedKey: 'ill.pair.eagle_phoenix.inverted',
		pairId: 'eagle_phoenix',
		seed: 11,
		style: '',
		mode: 'indep',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-eagle_phoenix-seed11-indep-final-view1.png',
		viewSha256: 'fc1765da03da392dc5f51316c1e09c4f35d9f09a765ce589e09a9848be399309',
		primeSha256: '5dc196dfd3098843cda779c13a1c6a4ee9a5b19772dd8d2f8d95a63cb93d3294'
	},
	{
		id: 'c-eagle-phoenix-37-joint-dreamd1',
		view: '/illusions/c-eagle-phoenix-37-joint-dreamd1-view.webp',
		prime: '/illusions/c-eagle-phoenix-37-joint-dreamd1-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.eagle_phoenix',
		uprightKey: 'ill.pair.eagle_phoenix.upright',
		invertedKey: 'ill.pair.eagle_phoenix.inverted',
		pairId: 'eagle_phoenix',
		seed: 37,
		style: '',
		mode: 'joint',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-eagle_phoenix-seed37-joint-dream_d1-view1.png',
		viewSha256: '06d6647a0916cb3a500a63ef6ce806ebbd2e2eed28c90109db4c49fd7dd990d9',
		primeSha256: '2d13d71cfab8167c7af1cb133406f13289bfaecfb1528a78bea33001066cb147'
	},
	{
		id: 'c-giraffe-penguin-calibration-11-joint-dreamd1',
		view: '/illusions/c-giraffe-penguin-calibration-11-joint-dreamd1-view.webp',
		prime: '/illusions/c-giraffe-penguin-calibration-11-joint-dreamd1-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.giraffe_penguin',
		uprightKey: 'ill.pair.giraffe_penguin.upright',
		invertedKey: 'ill.pair.giraffe_penguin.inverted',
		pairId: 'giraffe_penguin_calibration',
		seed: 11,
		style: '',
		mode: 'joint',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-giraffe_penguin_calibration-seed11-joint-dream_d1-view1.png',
		viewSha256: '695295cd440596a7fd975b5c421ee1033a7f57846fc95e98534aed157ee6e401',
		primeSha256: 'f851ddf6091fca9476f30bae12746fb59e7fd473275b59bbc520e2b0fd2a7e77'
	},
	{
		id: 'c-giraffe-penguin-calibration-37-joint-dreamd1',
		view: '/illusions/c-giraffe-penguin-calibration-37-joint-dreamd1-view.webp',
		prime: '/illusions/c-giraffe-penguin-calibration-37-joint-dreamd1-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.giraffe_penguin',
		uprightKey: 'ill.pair.giraffe_penguin.upright',
		invertedKey: 'ill.pair.giraffe_penguin.inverted',
		pairId: 'giraffe_penguin_calibration',
		seed: 37,
		style: '',
		mode: 'joint',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-giraffe_penguin_calibration-seed37-joint-dream_d1-view1.png',
		viewSha256: '091563046b8710ea68273d8d069ccb97b3e7aacdd63478ee57f7e956b8dda02b',
		primeSha256: '9ef3aa89e1de6dfdffd930d0e26be19f73cb07ee27e8a8b3640da64365af1aef'
	},
	{
		id: 'c-heron-swan-11-indep',
		view: '/illusions/c-heron-swan-11-indep-view.webp',
		prime: '/illusions/c-heron-swan-11-indep-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.heron_swan',
		uprightKey: 'ill.pair.heron_swan.upright',
		invertedKey: 'ill.pair.heron_swan.inverted',
		pairId: 'heron_swan',
		seed: 11,
		style: '',
		mode: 'indep',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-heron_swan-seed11-indep-final-view1.png',
		viewSha256: 'b753e11018e3b9247451681f88866063175d6b572d320b6ec4974d9029cbe09e',
		primeSha256: '031cd1033e3b76a581ee7341b49680859157622d0839d18e8504b3b55166a058'
	},
	{
		id: 'c-horse-wave-37-joint-dreamd1',
		view: '/illusions/c-horse-wave-37-joint-dreamd1-view.webp',
		prime: '/illusions/c-horse-wave-37-joint-dreamd1-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.horse_wave',
		uprightKey: 'ill.pair.horse_wave.upright',
		invertedKey: 'ill.pair.horse_wave.inverted',
		pairId: 'horse_wave',
		seed: 37,
		style: '',
		mode: 'joint',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-horse_wave-seed37-joint-dream_d1-view1.png',
		viewSha256: '52bad7c65bfa5c53180fcbb57e0def191d5206004e61a7439151117f50d90363',
		primeSha256: '53808b6e4067e6560121bcafabd8b08251881699fbbdfe2f422af3607723bab3'
	},
	{
		id: 'c-moose-butterfly-37-indep-sdsend',
		view: '/illusions/c-moose-butterfly-37-indep-sdsend-view.webp',
		prime: '/illusions/c-moose-butterfly-37-indep-sdsend-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.moose_butterfly',
		uprightKey: 'ill.pair.moose_butterfly.upright',
		invertedKey: 'ill.pair.moose_butterfly.inverted',
		pairId: 'moose_butterfly',
		seed: 37,
		style: '',
		mode: 'indep',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-moose_butterfly-seed37-indep-sds_end-view1.png',
		viewSha256: '75cbf9587647075c2814b5c697097f013e7be59331a9521030d5c2a0b7520267',
		primeSha256: '229aa8576b07369ec20d1c5a7bf50a4200525009c0154de3a25ad0dd107cc12c'
	},
	{
		id: 'c-moose-butterfly-37-indep-dreamd1',
		view: '/illusions/c-moose-butterfly-37-indep-dreamd1-view.webp',
		prime: '/illusions/c-moose-butterfly-37-indep-dreamd1-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.moose_butterfly',
		uprightKey: 'ill.pair.moose_butterfly.upright',
		invertedKey: 'ill.pair.moose_butterfly.inverted',
		pairId: 'moose_butterfly',
		seed: 37,
		style: '',
		mode: 'indep',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-moose_butterfly-seed37-indep-dream_d1-view1.png',
		viewSha256: '7316f351ed4013180a20c9befe00d182db7663518c02c2124b417261444d6bdd',
		primeSha256: '68e5e592d664a796247a5835d0896303df83839b06fc65540c1a9cc58f03c433'
	},
	{
		id: 'c-octopus-camel-37-joint-sdsend',
		view: '/illusions/c-octopus-camel-37-joint-sdsend-view.webp',
		prime: '/illusions/c-octopus-camel-37-joint-sdsend-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.octopus_camel',
		uprightKey: 'ill.pair.octopus_camel.upright',
		invertedKey: 'ill.pair.octopus_camel.inverted',
		pairId: 'octopus_camel',
		seed: 37,
		style: '',
		mode: 'joint',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-octopus_camel-seed37-joint-sds_end-view1.png',
		viewSha256: 'cca5a66ffcae29298c5a54b23ec7bb8a49ef03cc1ae9334d27ccb1fd77e0e6d0',
		primeSha256: 'c625fc0c9a53db45392fd6193d862542685f9da2e7f0350b1abd31031f6b4f45'
	},
	{
		id: 'c-penguin-bat-11-indep',
		view: '/illusions/c-penguin-bat-11-indep-view.webp',
		prime: '/illusions/c-penguin-bat-11-indep-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.penguin_bat',
		uprightKey: 'ill.pair.penguin_bat.upright',
		invertedKey: 'ill.pair.penguin_bat.inverted',
		pairId: 'penguin_bat',
		seed: 11,
		style: '',
		mode: 'indep',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-penguin_bat-seed11-indep-final-view1.png',
		viewSha256: 'b8f75f530a2efc214fab99aed0c6764a9f237091ab83851ba506e27844fc435e',
		primeSha256: '75ff7c5b557d1d1a22cecebeeac71f5026f38435352186e72d8358b910933f54'
	},
	{
		id: 'c-squirrel-pelican-11-indep-sdsend',
		view: '/illusions/c-squirrel-pelican-11-indep-sdsend-view.webp',
		prime: '/illusions/c-squirrel-pelican-11-indep-sdsend-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.squirrel_pelican',
		uprightKey: 'ill.pair.squirrel_pelican.upright',
		invertedKey: 'ill.pair.squirrel_pelican.inverted',
		pairId: 'squirrel_pelican',
		seed: 11,
		style: '',
		mode: 'indep',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-squirrel_pelican-seed11-indep-sds_end-view1.png',
		viewSha256: 'dfe339075b00fc5be11b534a8313b0293746a0787d9f8310e9b2c3c38e40833c',
		primeSha256: 'fbffb5bec585d2dd2aaf0ee23cbd47c7b028f8d241f0d2967aded6c22781224b'
	},
	{
		id: 'c-stag-oak-11-indep-sdsend',
		view: '/illusions/c-stag-oak-11-indep-sdsend-view.webp',
		prime: '/illusions/c-stag-oak-11-indep-sdsend-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.stag_oak',
		uprightKey: 'ill.pair.stag_oak.upright',
		invertedKey: 'ill.pair.stag_oak.inverted',
		pairId: 'stag_oak',
		seed: 11,
		style: '',
		mode: 'indep',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-stag_oak-seed11-indep-sds_end-view1.png',
		viewSha256: '8b81385f3412d143c810143bda6d27baf15e4e0f530c1a3bf26f17927cdf1d78',
		primeSha256: '422a4999b0d3c3c55463096594407294e69bf0a843e90f74a073a9d67b820294'
	},
	{
		id: 'c-wolf-raven-11-indep-sdsend',
		view: '/illusions/c-wolf-raven-11-indep-sdsend-view.webp',
		prime: '/illusions/c-wolf-raven-11-indep-sdsend-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.wolf_raven',
		uprightKey: 'ill.pair.wolf_raven.upright',
		invertedKey: 'ill.pair.wolf_raven.inverted',
		pairId: 'wolf_raven',
		seed: 11,
		style: '',
		mode: 'indep',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-wolf_raven-seed11-indep-sds_end-view1.png',
		viewSha256: '96b4b23b24475036e48e6c555893742cf634328f11d88df8dbb425986aab3b3b',
		primeSha256: 'e3e18100e2f06625bd75600063d72d7158d5568c87de01abab3ab08860d0f4b9'
	},
	{
		id: 'c-wolf-raven-23-joint-dreamd1',
		view: '/illusions/c-wolf-raven-23-joint-dreamd1-view.webp',
		prime: '/illusions/c-wolf-raven-23-joint-dreamd1-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.wolf_raven',
		uprightKey: 'ill.pair.wolf_raven.upright',
		invertedKey: 'ill.pair.wolf_raven.inverted',
		pairId: 'wolf_raven',
		seed: 23,
		style: '',
		mode: 'joint',
		arm: '',
		score: 5,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's5-wolf_raven-seed23-joint-dream_d1-view1.png',
		viewSha256: '65a7d4fc83403422d85ec41aedcc393558159671dce29015c9dc9b3bdeb8f5be',
		primeSha256: 'f7c5493d561ddb8909e84aa804774861696eeba3c96f3966407abf493ea362de'
	},
	{
		id: 'c-squirrel-pelican-23-joint',
		view: '/illusions/c-squirrel-pelican-23-joint-view.webp',
		prime: '/illusions/c-squirrel-pelican-23-joint-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.squirrel_pelican',
		uprightKey: 'ill.pair.squirrel_pelican.upright',
		invertedKey: 'ill.pair.squirrel_pelican.inverted',
		pairId: 'squirrel_pelican',
		seed: 23,
		style: '',
		mode: 'joint',
		arm: '',
		score: 4,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's4-squirrel_pelican-seed23-joint-final-view1.png',
		viewSha256: '6ebc07c6e23468953499a45684fcf0cfd318939c1e3b4138bdfefd1007e11487',
		primeSha256: 'ecf944146fdb4bff61fc25f3ec7097956bf47ff40abfe18706535aa7ec031cd6'
	},
	{
		id: 'c-wolf-raven-23-joint',
		view: '/illusions/c-wolf-raven-23-joint-view.webp',
		prime: '/illusions/c-wolf-raven-23-joint-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.wolf_raven',
		uprightKey: 'ill.pair.wolf_raven.upright',
		invertedKey: 'ill.pair.wolf_raven.inverted',
		pairId: 'wolf_raven',
		seed: 23,
		style: '',
		mode: 'joint',
		arm: '',
		score: 4,
		frame: 'unrated',
		exportTag: 'window-2026-08',
		sourcePng: 's4-wolf_raven-seed23-joint-final-view1.png',
		viewSha256: '9ad9012bf0c62215861bc7d9a4146c4266b9055cbd6f36e80115bb3a909a6213',
		primeSha256: 'c952ae92db763afce9aaa34d00e85068b7b1b39a79f64e5d56bd37c2bb8dfcf1'
	},
	{
		id: 'c-kingfisher-wisteria-11-joint',
		view: '/illusions/c-kingfisher-wisteria-11-joint-view.webp',
		prime: '/illusions/c-kingfisher-wisteria-11-joint-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.kingfisher_wisteria',
		uprightKey: 'ill.pair.kingfisher_wisteria.upright',
		invertedKey: 'ill.pair.kingfisher_wisteria.inverted',
		pairId: 'kingfisher_wisteria',
		seed: 11,
		style: 'oil',
		mode: 'joint',
		arm: 'joint',
		score: 4,
		frame: 'none',
		exportTag: 'window4-2026-08',
		sourcePng: 's4-kingfisher_wisteria-seed11-oil-joint-final-view1.png',
		viewSha256: 'fecf7dfbd9982c6d372e3692cfea5fd1109a6e9472f4ea55dc6cea779d63267e',
		primeSha256: 'e5ab722a2195c96e2861948235e8a2634764fd334bb8cf934df820f8a4257a9b'
	},
	{
		id: 'c-eagle-phoenix-149-joint',
		view: '/illusions/c-eagle-phoenix-149-joint-view.webp',
		prime: '/illusions/c-eagle-phoenix-149-joint-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.eagle_phoenix',
		uprightKey: 'ill.pair.eagle_phoenix.upright',
		invertedKey: 'ill.pair.eagle_phoenix.inverted',
		pairId: 'eagle_phoenix',
		seed: 149,
		style: 'oil',
		mode: 'joint',
		arm: 'joint',
		score: 4,
		frame: 'minor',
		exportTag: 'window6-2026-09',
		sourcePng: 's4-eagle_phoenix-seed149-oil-joint-final-view1.png',
		viewSha256: '3e7e8fb22f00919bddd5c4cfb81b809c3d5f970103225cc9686d69d15d1de012',
		primeSha256: 'b6e3dfd509052597988569af93cf6bc855b774d8cf4cfaa990fc3a74c47cf0b0'
	},
	{
		id: 'c-wolf-raven-181-indep',
		view: '/illusions/c-wolf-raven-181-indep-view.webp',
		prime: '/illusions/c-wolf-raven-181-indep-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.wolf_raven',
		uprightKey: 'ill.pair.wolf_raven.upright',
		invertedKey: 'ill.pair.wolf_raven.inverted',
		pairId: 'wolf_raven',
		seed: 181,
		style: 'oil',
		mode: 'indep',
		arm: 'indep',
		score: 5,
		frame: 'minor',
		exportTag: 'window7-2026-09',
		sourcePng: 's5-wolf_raven-seed181-oil-indep-final-view1.png',
		viewSha256: 'a04186f23f6926bd43a05cde06a885a9fbec2481b5441fe543f54cbb2834baf9',
		primeSha256: 'd8d505e1c44a8a4596bb0b2f129f7ecfa364b97a854a09c84cf9a8aa74c36734'
	},
	{
		id: 'c-wolf-raven-269-joint',
		view: '/illusions/c-wolf-raven-269-joint-view.webp',
		prime: '/illusions/c-wolf-raven-269-joint-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.wolf_raven',
		uprightKey: 'ill.pair.wolf_raven.upright',
		invertedKey: 'ill.pair.wolf_raven.inverted',
		pairId: 'wolf_raven',
		seed: 269,
		style: 'oil',
		mode: 'joint',
		arm: 'joint',
		score: 5,
		frame: 'none',
		exportTag: 'window7-2026-09',
		sourcePng: 's5-wolf_raven-seed269-oil-joint-final-view1.png',
		viewSha256: 'f2d8344e3d80431b31f016192d732dd5075e9dfea2afcb4c806bf7d836020793',
		primeSha256: '51ab3b84f5022e5ae390bd253868975e1bafb64c221ea0ec18a0fcf91dda4fea'
	},
	{
		id: 'c-eagle-phoenix-277-joint',
		view: '/illusions/c-eagle-phoenix-277-joint-view.webp',
		prime: '/illusions/c-eagle-phoenix-277-joint-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.eagle_phoenix',
		uprightKey: 'ill.pair.eagle_phoenix.upright',
		invertedKey: 'ill.pair.eagle_phoenix.inverted',
		pairId: 'eagle_phoenix',
		seed: 277,
		style: 'oil',
		mode: 'joint',
		arm: 'joint',
		score: 4,
		frame: 'none',
		exportTag: 'window7-2026-09',
		sourcePng: 's4-eagle_phoenix-seed277-oil-joint-final-view1.png',
		viewSha256: 'c557170a244fcb8e3dd60ebe2dc654ca041049784d25ed8d7368c45356b4df70',
		primeSha256: 'edf43a2aa9167b9fb5aaae63e512065bb17d16a3e698291dab926d9f45d96bcd'
	},
	{
		id: 'c-elephant-swan-211-joint',
		view: '/illusions/c-elephant-swan-211-joint-view.webp',
		prime: '/illusions/c-elephant-swan-211-joint-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.elephant_swan',
		uprightKey: 'ill.pair.elephant_swan.upright',
		invertedKey: 'ill.pair.elephant_swan.inverted',
		pairId: 'elephant_swan',
		seed: 211,
		style: 'oil',
		mode: 'joint',
		arm: 'joint',
		score: 4,
		frame: 'none',
		exportTag: 'window7-2026-09',
		sourcePng: 's4-elephant_swan-seed211-oil-joint-final-view1.png',
		viewSha256: '9c06665f5cedd8df4d11861f87c6c4b0116c66bc4461b03598842bb50609c101',
		primeSha256: 'e1f07170d9da42d95d2d83d4efeae129bf229bfc3e55ef9a89aeccab718cc7dc'
	},
	{
		id: 'c-wolf-raven-181-joint',
		view: '/illusions/c-wolf-raven-181-joint-view.webp',
		prime: '/illusions/c-wolf-raven-181-joint-prime.webp',
		viewWidth: 512,
		viewHeight: 512,
		primeWidth: 256,
		primeHeight: 256,
		titleKey: 'ill.pair.wolf_raven',
		uprightKey: 'ill.pair.wolf_raven.upright',
		invertedKey: 'ill.pair.wolf_raven.inverted',
		pairId: 'wolf_raven',
		seed: 181,
		style: 'oil',
		mode: 'joint',
		arm: 'joint',
		score: 4,
		frame: 'minor',
		exportTag: 'window7-2026-09',
		sourcePng: 's4-wolf_raven-seed181-oil-joint-final-view1.png',
		viewSha256: '3da42ffc43fe471a6d75765dde20b082e32308d19f2e458843ccecf2f94f2d88',
		primeSha256: 'e15d3802f35c719091ac53389dbf180b8b4f627a9c2a937cb8181ec1e2fbd47e'
	}
];

// One gallery for keepers and candidates alike, in a fixed shuffled order that keeps
// two cells of the same pair at least four places apart, so neighbours never echo.
const SHOWCASE_ORDER: readonly string[] = [
	'elephant-swan-seed37',
	'giraffe-penguin',
	'wolf-raven-seed53',
	'c-horse-wave-37-joint-dreamd1',
	'stag-oak',
	'c-eagle-phoenix-149-joint',
	'c-wolf-raven-181-indep',
	'c-dog-sloth-23-indep',
	'c-deer-turtle-11-joint-sdsend',
	'c-squirrel-pelican-23-joint',
	'moose-butterfly-seed11',
	'c-kingfisher-wisteria-11-joint',
	'elephant-swan-neg-on',
	'c-wolf-raven-269-joint',
	'c-eagle-phoenix-11-indep',
	'c-heron-swan-11-indep',
	'c-octopus-camel-37-joint-sdsend',
	'c-penguin-bat-11-indep',
	'c-wolf-raven-11-indep-sdsend',
	'c-moose-butterfly-37-indep-dreamd1',
	'c-giraffe-penguin-calibration-37-joint-dreamd1',
	'c-stag-oak-11-indep-sdsend',
	'c-squirrel-pelican-11-indep-sdsend',
	'c-wolf-raven-23-joint-dreamd1',
	'elephant-swan',
	'moose-butterfly-neg-on',
	'deer-turtle-neg-off',
	'c-wolf-raven-181-joint',
	'c-giraffe-penguin-calibration-11-joint-dreamd1',
	'c-eagle-phoenix-37-joint-dreamd1',
	'c-elephant-swan-211-joint',
	'c-wolf-raven-23-joint',
	'giraffe-penguin-seed37',
	'c-bear-salmon-37-joint-dreamd1',
	'eagle-phoenix',
	'wolf-raven',
	'elephant-swan-sketch',
	'c-moose-butterfly-37-indep-sdsend',
	'c-eagle-phoenix-277-joint',
	'wolf-raven-seed11'
];

export const ILLUSION_SHOWCASE: readonly (IllusionGalleryItem | IllusionCandidateItem)[] = [
	...ILLUSION_GALLERY,
	...ILLUSION_CANDIDATES
].sort((a, b) => SHOWCASE_ORDER.indexOf(a.id) - SHOWCASE_ORDER.indexOf(b.id));

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
	keeperCount: String(ILLUSION_EXPORT_KEEPERS),
	showcaseCount: String(ILLUSION_SHOWCASE.length),
	jointCount: String(ILLUSION_GALLERY.filter((item) => item.mode === 'joint').length),
	minScore: String(Math.min(...ILLUSION_GALLERY.map((item) => item.score))),
	score: String(ILLUSION_GALLERY[0].score)
} as const;

export function fillIllusionCopy(template: string): string {
	return template.replace(/\{(\w+)\}/g, (whole, name: string) => {
		return name in ILLUSION_COPY_VARS
			? ILLUSION_COPY_VARS[name as keyof typeof ILLUSION_COPY_VARS]
			: whole;
	});
}
