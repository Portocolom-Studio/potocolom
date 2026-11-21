// The header Connect control is disabled until a prompt is entered. Below the
// desktop width the prompt field sits under the canvases, so the reason has to
// travel with the button.

export function connectNeedsPrompt(input: {
	showConnect: boolean;
	canConnect: boolean;
	prompt: string;
}): boolean {
	return input.showConnect && !input.canConnect && input.prompt.trim() === '';
}
