from app.estimates import estimate_gpu_ms, schema_defaults


def test_schema_defaults_reads_property_defaults():
    parameters = {
        "type": "object",
        "properties": {
            "steps": {"type": "integer", "minimum": 4, "maximum": 8, "default": 8},
            "width": {"type": "integer", "enum": [1024], "default": 1024},
            "height": {"type": "integer", "enum": [1024], "default": 1024},
        },
    }
    assert schema_defaults(parameters) == {"steps": 8, "width": 1024, "height": 1024}


def test_estimate_gpu_ms_at_baseline_params():
    assert estimate_gpu_ms("sdxl-fast", {"width": 1024, "height": 1024, "steps": 8}) == 3736


def test_estimate_gpu_ms_ssd_1b_lightning_baseline():
    assert estimate_gpu_ms(
        "ssd-1b-lightning", {"width": 1024, "height": 1024, "steps": 8}) == 2777


def test_estimate_gpu_ms_scales_linearly_with_steps():
    baseline = estimate_gpu_ms("sdxl-fast", {"width": 1024, "height": 1024, "steps": 8})
    half_steps = estimate_gpu_ms("sdxl-fast", {"width": 1024, "height": 1024, "steps": 4})
    assert baseline == 3736
    assert half_steps == 1868


def test_estimate_gpu_ms_scales_with_pixel_count():
    full = estimate_gpu_ms("dreamshaper-lcm", {"width": 512, "height": 512, "steps": 4})
    quarter_pixels = estimate_gpu_ms("dreamshaper-lcm", {"width": 256, "height": 256, "steps": 4})
    assert full == 578
    assert quarter_pixels == 144


def test_estimate_gpu_ms_uses_schema_defaults_when_params_omitted():
    defaults = schema_defaults({
        "properties": {
            "steps": {"default": 20},
            "width": {"default": 1024},
            "height": {"default": 1024},
        },
    })
    assert estimate_gpu_ms("sdxl-base", defaults) == 15274


def test_estimate_gpu_ms_unknown_model_returns_none():
    assert estimate_gpu_ms("missing-model", {"width": 512, "height": 512, "steps": 4}) is None


def test_estimate_gpu_ms_requires_explicit_params():
    assert estimate_gpu_ms("sdxl-fast", {}) is None
    assert estimate_gpu_ms("sdxl-fast", {"steps": 8}) is None


def test_estimate_gpu_ms_factor_baseline_scales_with_factor():
    assert estimate_gpu_ms("realesrgan", {"factor": 2}) == 800
    assert estimate_gpu_ms("realesrgan", {"factor": 4}) == 3200
    assert estimate_gpu_ms("realesrgan", {}) == 800


def test_load_timings_survives_bad_json(tmp_path, monkeypatch):
    from app import estimates

    bad = tmp_path / "model_timings.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(estimates, "TIMINGS_PATH", bad)
    estimates._load_timings.cache_clear()
    assert estimates._load_timings() == {}
    estimates._load_timings.cache_clear()
