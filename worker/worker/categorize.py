"""Output categorization protocol seam for usage metrics.

The fixed labels are wired now, but classification is deliberately a no-op in
this pass: adding a second CLIP model would require an unapproved model download.
"""

LABELS = ("art", "photo_edit", "design", "character", "nsfw", "other")


def categorize_output(_image: bytes | None) -> tuple[str, None]:
    # A real CLIP categorizer will need the final realtime frame passed here.
    return "other", None
