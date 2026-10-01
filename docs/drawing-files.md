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

## Versions

Save writes version 4. Open accepts version 1 through version 4, so a file
saved by an earlier release still opens. Version 1 has stroke and clear
operations only; version 2 also accepts shape operations; version 3 also
accepts erase-region operations; version 4 also accepts a `pressure` field on
stroke points. A version 1 file containing a shape is invalid, a version 2
file containing an erase-region is invalid, and a version 1, 2 or 3 file
containing `pressure` on a point is invalid. Older readers reject a newer
version rather than opening an incomplete drawing.

The document uses a 512 by 512 coordinate space. It records draw and erase
strokes with stable IDs, colors, widths and ordered points, plus shapes,
region erases and clear operations. The undo position selects the visible
prefix of the operation list; later operations form the redo branch.

| Field | Value |
|---|---|
| `version` | `4` when saved; `1`, `2`, `3` or `4` when opened |
| `width`, `height` | `512` |
| `operations` | Ordered stroke, shape, erase-region and clear operations, including redo history |
| `cursor` | Integer from zero through the operation count |

A stroke has `kind: "stroke"`, a unique `id` starting with `operation-`,
`mode: "draw"` or `"erase"`, a six-digit hex `color`, integer `size` from
1 through 32, and nonempty `points` with numeric `x` and `y` coordinates. In
a version 4 file, a stroke point may also carry `pressure`, a finite number
from 0 through 1; a point without it paints at `size`, and a point with it
paints at `size * (0.25 + 0.75 * pressure)`, so a light touch narrows to a
quarter width and a full press reaches the brush's full size. The painted
width of a segment between two points uses the width at the segment's
starting point. `pressure` on a point in a version 1, 2 or 3 file, or on a
shape or erase-region point in any version, is invalid. Eraser color is
white (`#ffffff`). A clear has only `kind: "clear"` and `id`.
IDs contain digit groups separated by single hyphens after the prefix, with
at most 64 characters in total. They are opaque labels, not sequence numbers.
New IDs use random values, so imported IDs cannot exhaust an edit counter.
Point coordinates must be finite and between -1,000,000 and 1,000,000.
Points outside the visible canvas retain the geometry of a captured drag;
opening does not clamp or rescale them.

A shape has `kind: "shape"`, `id`, `shape: "line"`, `"rectangle"` or
`"ellipse"`, `color`, `size`, and a `points` array with exactly two points.
The points are the drag start and end. Line uses them as its endpoints;
rectangle and ellipse use them as opposite corners of their bounds. All
shapes are outlines. Reverse drags work the same way. Color, width and point
limits match strokes, and each shape counts as two points toward the limit.

One shape drag is one undo step. While the pointer moves, the preview uses
one temporary copy of the canvas from the start of that drag. Old preview
edges do not enter the journal or remain on the canvas. Save, pointer release
and lost pointer capture finish the visible shape, as they finish a stroke.

A region erase has `kind: "erase-region"`, a unique `id` and a `points` array
with 3 through 512 points, and no other keys. The points are the corners of a
closed polygon in the same coordinate space as stroke points, and each one is
checked by the same coordinate limits. Opening paints the polygon's interior
white (`#ffffff`), over whatever the earlier operations drew there, and leaves
everything outside it untouched; the fill closes the outline itself, so the
last point need not repeat the first. A self-intersecting outline fills by
the nonzero winding rule, so a loop wound against the outer one stays
unfilled. It is one undo step like any other, and its points count toward the
point limit. An erase-region is valid only in a version 3 or later file.

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
