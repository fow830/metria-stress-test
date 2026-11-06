"""Validate schema/fixtures against stress-run.schema.json v0.1.

Valid fixtures must validate. Invalid fixtures must fail — especially
aggregate-score / grade / band smuggling banned by the Ideal Score ban.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "stress-run.schema.json"
VALID_DIR = ROOT / "fixtures" / "valid"
INVALID_DIR = ROOT / "fixtures" / "invalid"

BANNED_AGGREGATE_FIELDS = (
    "ideal_score",
    "idealnost",
    "overall_score",
    "risk_score",
    "grade",
    "band",
)


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _json_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


def test_schema_file_exists() -> None:
    assert SCHEMA_PATH.is_file()


def test_fixture_directories_populated() -> None:
    assert _json_files(VALID_DIR), "expected at least one valid fixture"
    assert _json_files(INVALID_DIR), "expected at least one invalid fixture"


@pytest.mark.parametrize("path", _json_files(VALID_DIR), ids=lambda p: p.name)
def test_valid_fixtures_pass(validator: Draft202012Validator, path: Path) -> None:
    instance = json.loads(path.read_text(encoding="utf-8"))
    validator.validate(instance)


@pytest.mark.parametrize("path", _json_files(INVALID_DIR), ids=lambda p: p.name)
def test_invalid_fixtures_fail(validator: Draft202012Validator, path: Path) -> None:
    instance = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        validator.validate(instance)


@pytest.mark.parametrize("field", BANNED_AGGREGATE_FIELDS)
def test_banned_aggregate_field_has_rejecting_fixture(field: str) -> None:
    names = {p.stem for p in _json_files(INVALID_DIR)}
    assert field in names, f"missing invalid fixture for banned field {field!r}"


def test_valid_fixtures_never_carry_banned_fields() -> None:
    for path in _json_files(VALID_DIR):
        raw = path.read_text(encoding="utf-8")
        for field in BANNED_AGGREGATE_FIELDS:
            assert f'"{field}"' not in raw, f"{path.name} contains banned field {field}"


def test_valid_fixtures_keep_score_null() -> None:
    for path in _json_files(VALID_DIR):
        instance = json.loads(path.read_text(encoding="utf-8"))
        for lens in instance["lenses"]:
            assert lens["score"] is None, f"{path.name} lens {lens['id']} score must be null in v0.1"
            assert lens["score_reason"] == "uncalibrated"
