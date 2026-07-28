// Prompt length against a model's text encoder window (issue #148). Diffusers
// silently drops whatever does not fit, so the studio needs a number to warn
// with. Deliberately an estimate: the real CLIP tokenizer needs its ~1 MB BPE
// vocabulary shipped to the browser, and the issue asks only for accuracy
// within a few tokens. Prompts are mostly common words and comma separated
// tags, which CLIP encodes close to one token each, so counting words lands
// near the real figure. Non-Latin scripts tokenize far denser than this
// estimate suggests; the warning fires late for them rather than never.

/** The window is shared with the begin and end markers the encoder adds. */
const SPECIAL_TOKENS = 2;

/** Words no longer than this are usually a single BPE token. */
const WHOLE_WORD_CHARS = 6;

/** Every further run of this many characters costs roughly one more token. */
const EXTRA_TOKEN_CHARS = 4;

/**
 * Approximate tokens the encoder would spend on this prompt. An empty prompt
 * still costs the two markers, which is what the encoder itself would spend.
 */
export function estimatePromptTokens(prompt: string): number {
	// Punctuation is matched apart from words because CLIP gives the comma,
	// which is how prompts separate tags, a token of its own.
	const pieces = prompt.match(/[\p{L}\p{N}]+|[^\s\p{L}\p{N}]/gu) ?? [];
	let tokens = SPECIAL_TOKENS;
	for (const piece of pieces) {
		const overflow = Math.max(0, piece.length - WHOLE_WORD_CHARS);
		tokens += 1 + Math.floor(overflow / EXTRA_TOKEN_CHARS);
	}
	return tokens;
}

/**
 * Whether to tell the user their prompt tail will not reach the image. Takes
 * the count rather than the prompt so a caller rendering both the number and
 * the warning only scans the text once.
 *
 * A limit of 0 means the manifest never declared a window, so say nothing
 * rather than guess at one (see worker/worker/manifests.py).
 */
export function exceedsWindow(tokens: number, limit: number | undefined): boolean {
	return !!limit && limit > 0 && tokens > limit;
}
