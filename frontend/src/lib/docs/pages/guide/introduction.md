---
title: Introduction
subtitle: Sketch on a canvas and watch a model paint over your strokes.
tab: guide
group: Get started
order: 1
source: README.md "What makes it different", "Status"
---

potocolom is an open source platform for generative images that update while you draw. Your canvas frames stream to a GPU worker over one WebSocket. The generated frames stream back while you keep sketching.

Your images stay private by default. The studio has no public gallery, and asset URLs check the owner before they open.

## Pick your path

The guide is split into a few parts. Start with the one that fits you.

<div class="docs-cards">
<a class="docs-card" href="/docs/guide/quickstart"><span class="docs-card-icon" data-icon="rocket"></span><span class="docs-card-title">Quickstart</span><span class="docs-card-text">Install a release with one command, then open the studio.</span></a>
<a class="docs-card" href="/docs/guide/hardware-requirements"><span class="docs-card-icon" data-icon="cpu"></span><span class="docs-card-title">Hardware requirements</span><span class="docs-card-text">What a machine needs, and which GPUs work.</span></a>
<a class="docs-card" href="/docs/architecture/overview"><span class="docs-card-icon" data-icon="layers"></span><span class="docs-card-title">Architecture</span><span class="docs-card-text">How the realtime loop and GPU scheduling fit together.</span></a>
</div>

## Building on potocolom

<div class="docs-cards">
<a class="docs-card" href="/docs/api/overview"><span class="docs-card-icon" data-icon="code"></span><span class="docs-card-title">API</span><span class="docs-card-text">Endpoints, the realtime socket and account states.</span></a>
<a class="docs-card" href="/docs/contributing/local-development"><span class="docs-card-icon" data-icon="terminal"></span><span class="docs-card-title">Local development</span><span class="docs-card-text">Set up the dev loop and run the tests.</span></a>
<a class="docs-card" href="/docs/reference/decisions"><span class="docs-card-icon" data-icon="file"></span><span class="docs-card-title">Decisions</span><span class="docs-card-text">Why potocolom is built the way it is.</span></a>
</div>

## Status

potocolom is pre-alpha and under active development in the open. The walking skeleton, a real generation from start to end on a self-hosted install, is v0.1.1. That is the first published release.

> [!PLANNED]
> The managed cloud service is planned and not shipped. It opens later as an invite-only beta.
