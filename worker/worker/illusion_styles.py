"""Prompt style templates for diffusion illusions.

Torch-free on purpose. Campaign tests and plan builders wrap subjects in
these strings without importing worker.illusions, which needs the inference
extra. The optimizer fingerprint hashes this file with illusions.py and
illusion_experiment.py.
"""

from __future__ import annotations

STYLE_TEMPLATES: dict[str, str] = {
    "oil": "an oil painting of {}",
    "coherent_oil": "a coherent oil painting of {}",
    "pencil": "a detailed HB pencil sketch of {}",
    "editorial": "a centered editorial illustration of {} with a clear silhouette",
    # The wording of the one author-reference cell that has actually been run
    # and passed human review (the giraffe/penguin calibration smoke).
    "reference_sketch": "an intricate detailed hb pencil sketch of {}",
    # The heavier scaffolding the untested reference pairs carry, and that same
    # scaffolding with only the medium swapped. Holding the framing words
    # identical is what makes a pencil-versus-oil arm an attribution test of
    # the medium alone.
    "reference_pencil": (
        "a centered intricate HB pencil illustration of {}"
        ", full object, strong silhouette, isolated on plain warm paper"
    ),
    "reference_oil": (
        "a centered intricate oil painting of {}"
        ", full object, strong silhouette, isolated on plain warm canvas"
    ),
    # Window 3's wording screen, targeting the trade neither validated wording
    # wins: reference_sketch reads better raw (35 of 72 against 25) but loses 31 of
    # 72 to its own frames, while oil produces 0 frames in 78 and only ties on the
    # clean endpoint.
    #
    # BOTH were smoked at 1,500 steps before any block time was committed, on
    # moose_butterfly seed 11, against a plain-oil control at the same step count.
    # Chroma is the measure_colour statistic; the colour threshold is 20.
    #
    #   oil control       66.0 / 57.2   clean, full bleed
    #   monochrome_oil    18.6 / 27.0   WOODEN PICTURE FRAME, both arms
    #   charcoal           9.9 /  7.1   clean, faint edge only
    #
    # monochrome_oil is CUT and kept here only so it is not tried again. The word
    # "monochrome" works on colour - it cuts chroma by about 60% against the
    # control - but it also summons a framed painting, which is worse than anything
    # plain oil produced in 78 observations, and frame-cleanliness was the entire
    # reason to start from oil. "Monochrome oil painting" is auction-catalogue
    # vocabulary, where the images genuinely are photographs of framed paintings.
    #
    # It also refuted the mechanism this screen was built on. The claim was that
    # frames come from naming a PAPER-BOUND artifact. charcoal is paper-bound and
    # clean; monochrome_oil is canvas-bound and framed. Both directions fail, so
    # frame behaviour is a property of the SPECIFIC PHRASE and is not derivable
    # from the medium. Screen candidate strings with an 8-minute smoke; do not
    # reason about them.
    "monochrome_oil": "a monochrome oil painting of {}",
    # The live candidate, and it was included as the control. Pencil-grade
    # monochrome (9.9/7.1 against reference_sketch's 9.2 median) with none of
    # pencil's frames, which is what the hypothesis wanted from the other one.
    "charcoal": "a detailed charcoal drawing of {}",
    # P1 smoke, 2026-09-09. Ten replacement strings for reference_sketch, plus
    # the plain-oil control in the campaign (not listed here). Do not predict
    # which survive. Frame behaviour is a property of the specific phrase.
    "ink_wash": "an ink wash of {}",
    "graphite_drawing": "a graphite drawing of {}",
    "linocut": "a linocut of {}",
    "woodcut": "a woodcut of {}",
    "etching": "an etching of {}",
    "gouache": "a gouache painting of {}",
    "fresco": "a fresco of {}",
    "watercolor": "a watercolor of {}",
    "lithograph": "a lithograph of {}",
    "ink_drawing": "an ink drawing of {}",
}


def apply_style_template(subject: str, style: str | None) -> str:
    """Wrap a semantic subject in a style template. Subject text is preserved."""
    if style is None or style == "none":
        return subject
    if style not in STYLE_TEMPLATES:
        raise ValueError(f"unknown style {style!r}; choose from {sorted(STYLE_TEMPLATES)}")
    return STYLE_TEMPLATES[style].format(subject)
