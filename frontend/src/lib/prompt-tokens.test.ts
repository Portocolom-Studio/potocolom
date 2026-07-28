// node --test with the built in type stripping, so the estimator keeps a check
// without pulling a test framework into the frontend (see Makefile
// verify-frontend). Numbers are approximations, so these assert the behaviour
// the warning depends on rather than exact token counts.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { estimatePromptTokens, promptExceedsWindow } from './prompt-tokens.ts';

test('an empty prompt spends nothing', () => {
	assert.equal(estimatePromptTokens(''), 0);
	assert.equal(estimatePromptTokens('   '), 0);
});

test('short words cost about one token each plus the two markers', () => {
	assert.equal(estimatePromptTokens('a red cat'), 5);
});

test('the tag separator is counted', () => {
	assert.ok(estimatePromptTokens('a cat, a dog') > estimatePromptTokens('a cat a dog'));
});

test('long words cost more than short ones', () => {
	assert.ok(estimatePromptTokens('photorealistic') > estimatePromptTokens('photo'));
});

test('a typical studio prompt lands near the real CLIP count', () => {
	// The real tokenizer gives 24 for this; a warning tolerates a few either way.
	const prompt =
		'a photorealistic portrait of an astronaut riding a horse on mars, ' +
		'cinematic lighting, 8k, highly detailed';
	const tokens = estimatePromptTokens(prompt);
	assert.ok(tokens >= 20 && tokens <= 28, `estimated ${tokens}`);
});

test('a prompt well past the window is flagged', () => {
	const long = 'a detailed painting of a garden '.repeat(20);
	assert.ok(estimatePromptTokens(long) > 77);
	assert.ok(promptExceedsWindow(long, 77));
});

test('a prompt inside the window is not flagged', () => {
	assert.equal(promptExceedsWindow('a red cat on a fence', 77), false);
});

test('an undeclared window never warns', () => {
	// 0 is the manifest default: no claim about the encoder, so no warning.
	const long = 'a detailed painting of a garden '.repeat(20);
	assert.equal(promptExceedsWindow(long, 0), false);
	assert.equal(promptExceedsWindow(long, undefined), false);
});

test('a larger declared window tolerates a longer prompt', () => {
	// What a T5 based model buys: same prompt, no warning (issue #148).
	const long = 'a detailed painting of a garden '.repeat(20);
	assert.ok(promptExceedsWindow(long, 77));
	assert.equal(promptExceedsWindow(long, 512), false);
});
