"""Test adequacy adapter — discover and optionally run pytest in a bounded process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from metria.discovery import SKIP_DIRS, count_loc, find_test_layout
from metria.model import dimension, not_measured


def _collect_only(root: Path, timeout_s: int) -> Tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _run_pytest(root: Path, timeout_s: int) -> Tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--maxfail=20"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def run_test_adequacy(
    root: Path,
    evidence_dir: Path,
    *,
    run_tests: bool = True,
    collect_timeout_s: int = 60,
    run_timeout_s: int = 180,
) -> Dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    loc = count_loc(root)
    layout = find_test_layout(root)
    scope = {
        "languages_analyzed": ["python"] if loc["py_files"] else [],
        "loc_analyzed": loc["py_loc"],
        "loc_total": loc["total_loc"],
        "excluded_paths": sorted(SKIP_DIRS),
        "exclusion_reasons": {d: "tooling_or_vcs" for d in sorted(SKIP_DIRS)},
    }
    tools = [{"name": "pytest", "version": _pytest_version(), "ruleset_digest": None}]

    (evidence_dir / "layout.json").write_text(json.dumps(layout, indent=2), encoding="utf-8")

    if layout["test_file_count"] == 0 and not layout["has_tests_dir"]:
        finding = {
            "fingerprint": "adeq:no_tests",
            "title": "No automated tests discovered",
            "severity": "high",
            "detectors": ["pytest-discovery"],
            "confirmation": "unconfirmed",
            "lens_routing_rule": "test_oracle_gap_to_test_adequacy",
            "evidence_refs": ["test_adequacy/layout.json"],
        }
        return dimension(
            lens_id="test_adequacy",
            status="fail",
            confidence="high",
            findings=[finding],
            raw_metrics={**layout, "strong_oracle": False, "suite": "absent"},
            evidence_refs=["test_adequacy/layout.json"],
            analysis_scope=scope,
            tools=tools,
            maturity="core",
        )

    # Collect
    try:
        code, out, err = _collect_only(root, collect_timeout_s)
    except subprocess.TimeoutExpired:
        (evidence_dir / "collect.txt").write_text("TIMEOUT\n", encoding="utf-8")
        return not_measured(
            "test_adequacy",
            null_reason="timeout",
            raw_metrics={**layout, "suite": "collect_timeout"},
            evidence_refs=["test_adequacy/layout.json", "test_adequacy/collect.txt"],
            analysis_scope=scope,
            tools=tools,
        )
    (evidence_dir / "collect.txt").write_text(out + "\n" + err, encoding="utf-8")
    if code != 0:
        (evidence_dir / "collect_failed.txt").write_text(out + "\n" + err, encoding="utf-8")
        return not_measured(
            "test_adequacy",
            null_reason="build_failed",
            raw_metrics={**layout, "suite": "collect_failed", "collect_exit": code},
            evidence_refs=[
                "test_adequacy/layout.json",
                "test_adequacy/collect.txt",
                "test_adequacy/collect_failed.txt",
            ],
            analysis_scope=scope,
            tools=tools,
        )

    if not run_tests:
        return dimension(
            lens_id="test_adequacy",
            status="warn",
            confidence="medium",
            findings=[
                {
                    "fingerprint": "adeq:collect_only",
                    "title": "Tests collected but not executed (run_tests=false)",
                    "severity": "low",
                    "detectors": ["pytest-discovery"],
                    "confirmation": "unconfirmed",
                    "lens_routing_rule": "test_oracle_gap_to_test_adequacy",
                    "evidence_refs": ["test_adequacy/collect.txt"],
                }
            ],
            raw_metrics={**layout, "suite": "collected_only", "strong_oracle": False},
            evidence_refs=["test_adequacy/layout.json", "test_adequacy/collect.txt"],
            analysis_scope=scope,
            tools=tools,
            maturity="core",
        )

    try:
        code, out, err = _run_pytest(root, run_timeout_s)
    except subprocess.TimeoutExpired:
        (evidence_dir / "pytest.txt").write_text("TIMEOUT\n", encoding="utf-8")
        return not_measured(
            "test_adequacy",
            null_reason="timeout",
            raw_metrics={**layout, "suite": "run_timeout"},
            evidence_refs=["test_adequacy/layout.json", "test_adequacy/pytest.txt"],
            analysis_scope=scope,
            tools=tools,
        )

    (evidence_dir / "pytest.txt").write_text(out + "\n" + err, encoding="utf-8")
    findings: List[Dict[str, Any]] = []
    if code != 0:
        findings.append(
            {
                "fingerprint": "adeq:suite_failed",
                "title": f"pytest failed (exit {code})",
                "severity": "high",
                "detectors": ["pytest"],
                "confirmation": "unconfirmed",
                "lens_routing_rule": "test_oracle_gap_to_test_adequacy",
                "evidence_refs": ["test_adequacy/pytest.txt"],
            }
        )
        status = "fail"
        suite = "failed"
    else:
        # green suite but no strong oracle in v0.1 → warn
        findings.append(
            {
                "fingerprint": "adeq:no_strong_oracle",
                "title": "Suite green but no strong oracle (mutation/PBT) measured",
                "severity": "medium",
                "detectors": ["pytest"],
                "confirmation": "unconfirmed",
                "lens_routing_rule": "test_oracle_gap_to_test_adequacy",
                "evidence_refs": ["test_adequacy/pytest.txt"],
            }
        )
        status = "warn"
        suite = "green"

    return dimension(
        lens_id="test_adequacy",
        status=status,
        confidence="medium",
        findings=findings,
        raw_metrics={
            **layout,
            "suite": suite,
            "pytest_exit": code,
            "strong_oracle": False,
        },
        evidence_refs=[
            "test_adequacy/layout.json",
            "test_adequacy/collect.txt",
            "test_adequacy/pytest.txt",
        ],
        analysis_scope=scope,
        tools=tools,
        maturity="core",
    )


def _pytest_version() -> str:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return (proc.stdout or proc.stderr or "unknown").strip()
    except Exception:
        return "unknown"
