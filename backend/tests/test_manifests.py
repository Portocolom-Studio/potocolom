from app.manifests import (
    Manifest,
    _params_validator,
    parse_manifests,
    validate_params,
)

SCHEMA = {
    "type": "object",
    "properties": {"prompt": {"type": "string"}},
    "required": ["prompt"],
}


def test_validate_params_accepts_valid_request():
    manifest = Manifest(id="m1", name="M1", capabilities=["text_to_image"],
                        parameters=SCHEMA)
    assert validate_params(manifest, {"prompt": "hello"}) is None


def test_validate_params_rejects_invalid_request():
    manifest = Manifest(id="m1", name="M1", capabilities=["text_to_image"],
                        parameters=SCHEMA)
    assert validate_params(manifest, {}) == "'prompt' is a required property"


def test_params_validator_is_cached():
    schema_json = '{"properties":{"prompt":{"type":"string"}},"required":["prompt"],"type":"object"}'
    first = _params_validator(schema_json)
    second = _params_validator(schema_json)
    assert first is second


def test_validate_params_accepts_on_invalid_schema():
    manifest = Manifest(id="m1", name="M1", capabilities=["text_to_image"],
                        parameters={"required": "prompt"})
    assert validate_params(manifest, {"anything": True}) is None


def test_parse_manifests_rejects_upscale_mixed_with_diffusion():
    try:
        parse_manifests([{
            "id": "bad",
            "name": "Bad",
            "capabilities": ["upscale", "image_to_image"],
            "parameters": {},
        }])
    except ValueError as error:
        assert "upscale cannot combine" in str(error)
    else:
        raise AssertionError("expected ValueError")
