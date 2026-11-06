"""Unit tests for status-first model + schema containment of CLI output."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from metria.model import aggregate_overall, dimension, undeclared
from metria.render import render_report
from metria.runner import load_policy, run_subject, policy_digest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schema" / "stress-run.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def test_score_always_null_in_dimension() -> None:
    d = dimension(lens_id="structural", status="pass")
    assert d["score"] is None
    assert d["score_reason"] == "uncalibrated"


def test_undeclared_maps_null_display() -> None:
    d = undeclared("architectural")
    assert d["status"] == "null"
    assert d["null_owner"] == "subject"
    assert d["null_display"] == "UNDECLARED"


def test_aggregate_hard_gate_wins() -> None:
    lenses = {
        "structural": dimension(lens_id="structural", status="pass"),
        "supply_chain": dimension(
            lens_id="supply_chain",
            status="warn",
            hard_gate_failures=["KEV"],
        ),
    }
    assert aggregate_overall(lenses) == "fail"


def test_report_never_says_bare_null() -> None:
    lenses = {
        "structural": dimension(lens_id="structural", status="pass"),
        "supply_chain": dimension(lens_id="supply_chain", status="pass"),
        "controls": undeclared("controls"),
        "test_adequacy": dimension(lens_id="test_adequacy", status="warn"),
        "architectural": undeclared("architectural"),
        "normative": undeclared("normative", maturity="experimental"),
    }
    run = {
        "schema_version": "0.1",
        "overall": "warn",
        "policy_digest": "sha256:" + "a" * 64,
        "subject": {"commit_sha": "abc1234deadbeef"},
        "lenses": lenses,
        "top_risks": [],
        "evidence_pack": {"sha256": "b" * 64},
    }
    text = render_report(run)
    assert " UNDECLARED" in text or "UNDECLARED" in text
    assert "\nNULL\n" not in text
    assert "ideal_score" not in text
    assert " grade " not in text.lower()


def test_run_on_tiny_local_subject(tmp_path: Path) -> None:
    subj = tmp_path / "subj"
    subj.mkdir()
    subprocess.check_call(["git", "init", "-q"], cwd=str(subj))
    subprocess.check_call(["git", "config", "user.email", "t@example.com"], cwd=str(subj))
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=str(subj))
    (subj / "hello.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (subj / "test_hello.py").write_text(
        "from hello import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    subprocess.check_call(["git", "add", "."], cwd=str(subj))
    subprocess.check_call(["git", "commit", "-q", "-m", "init"], cwd=str(subj))

    policy = load_policy(ROOT / "policy" / "cold_external.yaml")
    out = tmp_path / "out"
    run = run_subject(
        subj,
        repo_url="local://subj",
        policy=policy,
        out_dir=out,
        query_osv=False,
        run_tests=True,
    )
    VALIDATOR.validate(run)
    assert "score" not in run  # aggregate score forbidden; only per-lens null score
    assert "ideal_score" not in run
    assert "grade" not in run
    assert "band" not in run
    assert run["lenses"]["structural"]["score"] is None
    assert run["lenses"]["structural"]["score_reason"] == "uncalibrated"
    assert run["lenses"]["controls"]["null_display"] == "UNDECLARED"
    assert run["lenses"]["architectural"]["null_display"] == "UNDECLARED"
    assert run["lenses"]["normative"]["maturity"] == "experimental"
    assert run["lenses"]["normative"]["null_display"] == "UNDECLARED"
    assert (out / "stress-run.json").is_file()
    assert (out / "report.txt").is_file()
    assert (out / "evidence.zip").is_file()
    assert "ideal_score" not in (out / "report.txt").read_text(encoding="utf-8")


def test_cli_module_imports() -> None:
    import metria.cli as cli

    assert cli.build_parser()


def test_policy_digest_stable() -> None:
    policy = load_policy(ROOT / "policy" / "cold_external.yaml")
    assert policy_digest(policy).startswith("sha256:")
    assert policy_digest(policy) == policy_digest(policy)
