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


def test_prompt_token_limit_crosses_the_wire():
    """The studio warning (issue #148) reads this off GET /api/v1/models, so it
    has to survive parsing rather than be dropped as an unknown worker field."""
    parsed = parse_manifests([{
        "id": "m1",
        "name": "M1",
        "capabilities": ["text_to_image"],
        "parameters": SCHEMA,
        "prompt_token_limit": 77,
        "source": "worker/side/only",
    }])
    assert parsed[0].prompt_token_limit == 77


def test_prompt_token_limit_defaults_to_undeclared():
    """A worker that never declares a window leaves the studio silent instead
    of asserting a CLIP limit the model may not have."""
    parsed = parse_manifests([
        {"id": "m1", "name": "M1", "capabilities": ["text_to_image"], "parameters": SCHEMA},
    ])
    assert parsed[0].prompt_token_limit == 0


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


def test_unresolvable_schema_reference_does_not_escape():
    # jsonschema raises Unresolvable past ValidationError, so an unhandled one
    # would reach the request handler as a 500 (issue #203).
    manifest = Manifest(
        id="broken", name="broken", capabilities=["text_to_image"],
        parameters={"type": "object",
                    "properties": {"prompt": {"$ref": "https://example.com/a.json"}}},
    )
    assert validate_params(manifest, {"prompt": "x"}) is None


def test_schema_too_deep_to_walk_fails_closed():
    """RecursionError must not read as "params acceptable".

    validate_params is the only input-validation gate on generations, upscale
    and the realtime open. Returning None when it could not walk the schema
    would skip that gate entirely, which is worse than the 500 it replaced.
    """
    schema: dict = {"type": "string"}
    for _ in range(600):
        schema = {"type": "array", "items": schema}
    manifest = Manifest(id="deep", name="deep", capabilities=["text_to_image"],
                        parameters={"type": "object", "properties": {"prompt": schema}})
    assert "too deeply" in (validate_params(manifest, {"prompt": "x"}) or "")


def test_self_referential_schema_reference_does_not_escape():
    # A cyclic $ref with no base case raises RecursionError, not
    # ValidationError, so an unhandled one reaches the handler as a 500. It
    # fails closed rather than accepting unchecked: nothing can be validated
    # against a schema that cannot be walked (issue #203).
    manifest = Manifest(
        id="cyclic", name="cyclic", capabilities=["text_to_image"],
        parameters={"$defs": {"a": {"$ref": "#/$defs/a"}},
                    "properties": {"prompt": {"$ref": "#/$defs/a"}}},
    )
    assert "too deeply" in (validate_params(manifest, {"prompt": "x"}) or "")
