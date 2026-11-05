---
title: Decisions
subtitle: How potocolom records its design choices, and where the record lives.
tab: reference
group: Project
order: 1
source: docs/decisions.md "Design decisions" (format and entry count); CONTRIBUTING.md "Contributing" and "Ground rules" (how to propose a change)
---

potocolom keeps its design choices in one file, docs/decisions.md. Read it to learn why the project works the way it does. Add to it when the project agrees on a new choice.

The record holds more than 190 entries. They cover the API server and frontend stack, the inference worker, accounts, billing, licensing, cloud hosting, and the realtime path. This page does not copy the entries. Read them on GitHub, linked at the end.

## What an entry holds

The top of the file states the format: one section per decision, with what was chosen, why, and the main alternative that was rejected. Each entry is a heading followed by prose. There are no form fields and no template.

- **Decision:** the heading and the first paragraph say what was chosen.
- **Reasons:** the paragraphs after that say why. They often name the trade-offs and the cost of the choice.
- **Rejected alternative:** a paragraph that starts with "Rejected alternative" or "Rejected alternatives" names the option that was not taken and says why it lost.

Later reviews go into the same entry. A choice that was re-examined and kept says so in the entry.

## Status notes

Some entries have a dated "Shipped status" note. The note says what is built today. If the design text and the status note disagree, the note describes the code that runs now. A design that is not built yet is written as planned, not as available.

## Superseded entries

When a decision changes, the old entry stays in the file. The entry gets a note at its top that says it is Superseded and names the newer entry that replaced it. The old text stays on purpose, so the record shows what was decided before and why it changed.

> [!NOTE]
> Read an entry marked Superseded for history only. Its present-tense claims no longer apply.

## Propose a new entry

A new entry follows the same path as any other change. The steps below come from CONTRIBUTING.md.

1. Open an issue that describes the change and the reason for it.
2. Make a short-lived branch from main.
3. Add the entry to docs/decisions.md. State the decision, the reasons, and the main alternative you rejected. If the change replaces an older entry, mark that entry Superseded and name the new one.
4. Run `make verify` locally. It runs what CI runs.
5. Open a PR into main. Commit with `git commit -s`, so each commit carries a `Signed-off-by` line. A PR without the sign-off cannot be merged.

## Read the record

The file lives on GitHub: [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md). The contribution rules are in [CONTRIBUTING.md](https://github.com/Portocolom-Studio/potocolom/blob/main/CONTRIBUTING.md).
