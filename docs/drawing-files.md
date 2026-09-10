# Drawing files

Save drawing downloads `drawing.potocolom.json`. Open drawing reads a file
from the device and replaces the current drawing. Save work you want to keep
before opening another file or leaving the drawing panel. There is no auto-save,
browser draft store or cloud sync.

The file stores the drawing operations, their order, the current undo position
and the redo branch. Opening it restores the visible bitmap and lets the user
continue undoing, redoing and drawing. A new edit after undo replaces the redo
branch, as it does before saving. Clear is an undoable operation.

The file does not store prompts, model choices, generated images, account data
or live session IDs. It works without a GPU connection. Saving does not send a
live frame. Opening a file while connected sends the changed canvas through
the existing complete-WebP path, including when the opened drawing is blank.

## Version 1

The document uses a 512 by 512 coordinate space. It records draw and erase
strokes with stable IDs, colors, widths and ordered points, plus clear
operations. The undo position selects the visible prefix of the operation
list; later operations form the redo branch.

| Field | Value |
|---|---|
| `version` | `1` |
| `width`, `height` | `512` |
| `operations` | Ordered stroke and clear operations, including redo history |
| `cursor` | Integer from zero through the operation count |

A stroke has `kind: "stroke"`, a unique `id` starting with `operation-`,
`mode: "draw"` or `"erase"`, a six-digit hex `color`, integer `size` from
1 through 32, and nonempty `points` with numeric `x` and `y` coordinates.
Eraser color is white (`#ffffff`). A clear has only `kind: "clear"` and `id`.
IDs contain digit groups separated by single hyphens after the prefix, with
at most 64 characters in total. They are opaque labels, not sequence numbers.
New IDs use random values, so imported IDs cannot exhaust an edit counter.
Point coordinates must be finite and between -1,000,000 and 1,000,000.
Points outside the visible canvas retain the geometry of a captured drag;
opening does not clamp or rescale them.

Limits apply to both saving and opening: 8 MiB of UTF-8 JSON, 10,000
operations and 200,000 points in the full journal, including the redo branch.
Saving a drawing above these limits reports an error and keeps the drawing
in memory. It does not download a truncated or unreadable file.

Opening checks the whole document before replacing any current artwork. An
unsupported version, invalid operation or file that exceeds the limits reports
an error and leaves the current drawing and history unchanged. Drawing
controls pause while a file is read. A late file read cannot replace a later
open or a document in a new panel.

The file contains no raster checkpoints. Replay uses the same painting rules
as live drawing. Runtime checkpoints speed up undo and redo and can be dropped
without losing operations. Pixel comparisons in tests use the same browser;
different browser rasterizers can produce different antialiasing.

Higher-resolution refine and persisted compressed checkpoints remain in #54.
