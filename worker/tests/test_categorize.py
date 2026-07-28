from worker.categorize import LABELS, categorize_output


def test_categorizer_noop_uses_fixed_other_label_without_fake_score():
    assert LABELS == ("art", "photo_edit", "design", "character", "nsfw", "other")
    assert categorize_output(None) == ("other", None)
