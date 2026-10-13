"""Validate payloads against docs/protocol/v1.openapi.yaml component schemas."""
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "protocol" / "v1.openapi.yaml"
_SPEC = yaml.safe_load(SPEC_PATH.read_text())


def assert_matches(schema_name: str, instance) -> None:
    # Root the $ref at a document that carries `components`, so internal refs resolve.
    schema = {"$ref": f"#/components/schemas/{schema_name}", "components": _SPEC["components"]}
    Draft202012Validator(schema).validate(instance)
