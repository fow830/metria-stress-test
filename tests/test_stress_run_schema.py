"""Validate StressRun schema fixtures (Commit #0).

Valid fixtures must pass. Negative fixtures that smuggle Ideal Score,
grade, band, blended protectability weights, or unattributed nulls must fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema" / "stress-run.schema.json"
FIXTURES = ROOT / "schema" / "fixtures"

VALID = [
    "valid-minimal.json",
]

INVALID = [
    "invalid-ideal-score.json",
    "invalid-grade.json",
    "invalid-band.json",
    "invalid-blended-protectability-weights.json",
    "invalid-null-without-reason.json",
]


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", VALID)
def test_valid_fixtures_pass(validator: Draft202012Validator, name: str) -> None:
    validator.validate(_load(name))


@pytest.mark.parametrize("name", INVALID)
def test_invalid_fixtures_fail(validator: Draft202012Validator, name: str) -> None:
    with pytest.raises(ValidationError):
        validator.validate(_load(name))


def test_score_must_be_null(validator: Draft202012Validator) -> None:
    doc = _load("valid-minimal.json")
    doc["lenses"]["structural"]["score"] = 72
    with pytest.raises(ValidationError):
        validator.validate(doc)


def test_score_reason_must_be_uncalibrated(validator: Draft202012Validator) -> None:
    doc = _load("valid-minimal.json")
    doc["lenses"]["structural"]["score_reason"] = "calibrated_v3"
    with pytest.raises(ValidationError):
        validator.validate(doc)


def test_forbidden_aggregate_fields(validator: Draft202012Validator) -> None:
    for field, value in [
        ("ideal_score", 90),
        ("idealnost", 0.9),
        ("overall_score", 88),
        ("risk_score", 12),
        ("grade", "A"),
        ("band", "B"),
    ]:
        doc = _load("valid-minimal.json")
        doc[field] = value
        with pytest.raises(ValidationError):
            validator.validate(doc)


def test_normative_must_be_experimental(validator: Draft202012Validator) -> None:
    doc = _load("valid-minimal.json")
    doc["lenses"]["normative"]["maturity"] = "core"
    with pytest.raises(ValidationError):
        validator.validate(doc)


def test_null_owner_subject_requires_undeclared(
    validator: Draft202012Validator,
) -> None:
    doc = _load("valid-minimal.json")
    lens = doc["lenses"]["architectural"]
    lens["null_owner"] = "subject"
    lens["null_display"] = "NOT MEASURED"
    with pytest.raises(ValidationError):
        validator.validate(doc)


def test_fixture_inventory_complete() -> None:
    names = {p.name for p in FIXTURES.glob("*.json")}
    expected = set(VALID) | set(INVALID)
    assert names == expected
