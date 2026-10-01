import io

import pytest
from PIL import Image, ImageDraw

from worker import categorize as categorizer
from worker.categorize import LABELS, categorize_output


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_siglip2_categorizes_a_flat_colour_and_a_shape_on_cpu(monkeypatch) -> None:
    torch = pytest.importorskip("torch")
    pytest.importorskip("transformers")

    # Fresh state so this test always reaches the real pinned loader, and the
    # switch flips back off afterwards for any test that follows it.
    monkeypatch.setattr(categorizer, "_enabled", False)
    monkeypatch.setattr(categorizer, "_loaded", None)
    monkeypatch.setattr(categorizer, "_load_failure_logged", False)
    categorizer.enable_categorizer()

    flat = _png(Image.new("RGB", (64, 64), (196, 48, 34)))
    shape = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(shape).rectangle((12, 20, 52, 44), fill=(20, 20, 20))

    results = [categorize_output(flat), categorize_output(_png(shape))]

    for label, score in results:
        assert label in LABELS
        assert score is not None, "the pinned model did not load; see the load log"
        assert 0.0 <= score <= 1.0
    model, _processor, text_embeds = categorizer._loaded
    assert next(model.parameters()).device.type == "cpu"
    assert next(model.parameters()).dtype == torch.float32
    assert text_embeds.shape[0] == len(LABELS) - 1  # "other" has no prompt
