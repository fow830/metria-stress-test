"""Test adequacy — static discovery always; pytest only in scrubbed isolated venv."""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from metria.discovery import count_loc, existing_skip_dirs, find_test_layout
from metria.model import dimension, not_measured
from metria.sandbox import (
    cleanup_dir,
    create_subject_venv,
    install_subject_test_deps,
    run_scrubbed,
    scrub_env,
    scrub_text_paths,
)

_ENV_ERROR_HINTS = re.compile(
    r"(ModuleNotFoundError|ImportError|fixture ['\"].+['\"] not found|"
    r"ERROR collecting|ERROR at setup|Skipped: could not import|"
    r"pytest-httpbin|_dbm|No module named)",
    re.I,
)


def _runner_versions() -> Dict[str, str]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "implementation": platform.python_implementation(),
    }


def _pytest_version(py: Optional[Path] = None) -> str:
    exe = str(py) if py else sys.executable
    try:
        proc = subprocess.run(
            [exe, "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return (proc.stdout or proc.stderr or "unknown").strip()
    except Exception:
        return "unknown"


def _looks_like_env_failure(out: str, err: str, code: int) -> bool:
    blob = f"{out}\n{err}"
    if _ENV_ERROR_HINTS.search(blob):
        return True
    # pytest usage / internal / collect errors
    if code in {2, 3, 4}:
        return True
    # ERROR lines without FAILED assertions → env/setup
    if "ERROR" in blob and "FAILED" not in blob:
        return True
    return False


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
    excluded = existing_skip_dirs(root)
    runner = _runner_versions()
    scope = {
        "languages_analyzed": ["python"] if loc["py_files"] else [],
        "loc_analyzed": loc["py_loc"],
        "loc_total": loc["total_loc"],
        "excluded_paths": excluded,
        "exclusion_reasons": {d: "tooling_or_vcs" for d in excluded},
    }
    tools = [{"name": "pytest", "version": _pytest_version(), "ruleset_digest": None}]

    (evidence_dir / "layout.json").write_text(json.dumps(layout, indent=2), encoding="utf-8")
    (evidence_dir / "runner.json").write_text(json.dumps(runner, indent=2), encoding="utf-8")

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
            raw_metrics={**layout, "strong_oracle": False, "suite": "absent", "runner": runner},
            evidence_refs=["test_adequacy/layout.json", "test_adequacy/runner.json"],
            analysis_scope=scope,
            tools=tools,
            maturity="core",
        )

    # B1.2: --no-tests → zero subject code execution (static layout only).
    if not run_tests:
        return dimension(
            lens_id="test_adequacy",
            status="warn",
            confidence="medium",
            findings=[
                {
                    "fingerprint": "adeq:static_only",
                    "title": "Tests discovered statically; subject code not executed (--no-tests)",
                    "severity": "low",
                    "detectors": ["pytest-discovery"],
                    "confirmation": "unconfirmed",
                    "lens_routing_rule": "test_oracle_gap_to_test_adequacy",
                    "evidence_refs": ["test_adequacy/layout.json"],
                }
            ],
            raw_metrics={
                **layout,
                "suite": "static_only",
                "strong_oracle": False,
                "subject_code_executed": False,
                "runner": runner,
            },
            evidence_refs=["test_adequacy/layout.json", "test_adequacy/runner.json"],
            analysis_scope=scope,
            tools=tools,
            maturity="core",
        )

    # B1.3 / B3: isolated venv + scrubbed env + install subject test deps.
    work = Path(tempfile.mkdtemp(prefix="metria-adeq-"))
    home = work / "home"
    home.mkdir()
    try:
        env = scrub_env(home=home)
        try:
            py, pip = create_subject_venv(work)
        except Exception as exc:  # noqa: BLE001
            (evidence_dir / "venv_error.txt").write_text(str(exc), encoding="utf-8")
            return not_measured(
                "test_adequacy",
                null_reason="test_env_unavailable",
                raw_metrics={**layout, "suite": "venv_failed", "runner": runner, "error": str(exc)},
                evidence_refs=[
                    "test_adequacy/layout.json",
                    "test_adequacy/runner.json",
                    "test_adequacy/venv_error.txt",
                ],
                analysis_scope=scope,
                tools=tools,
            )

        env = scrub_env(home=home, extra={"VIRTUAL_ENV": str(work / "venv")})
        install_log = install_subject_test_deps(root, pip, env, timeout_s=max(run_timeout_s, 600))
        (evidence_dir / "install.json").write_text(json.dumps(install_log, indent=2), encoding="utf-8")
        tools = [{"name": "pytest", "version": _pytest_version(py), "ruleset_digest": None}]

        # Collect
        try:
            proc = run_scrubbed(
                [str(py), "-m", "pytest", "--collect-only", "-q"],
                cwd=root,
                env=env,
                timeout=collect_timeout_s,
            )
        except subprocess.TimeoutExpired:
            (evidence_dir / "collect.txt").write_text("TIMEOUT\n", encoding="utf-8")
            return not_measured(
                "test_adequacy",
                null_reason="timeout",
                raw_metrics={**layout, "suite": "collect_timeout", "runner": runner},
                evidence_refs=[
                    "test_adequacy/layout.json",
                    "test_adequacy/runner.json",
                    "test_adequacy/collect.txt",
                    "test_adequacy/install.json",
                ],
                analysis_scope=scope,
                tools=tools,
            )

        out = scrub_text_paths(proc.stdout or "", root)
        err = scrub_text_paths(proc.stderr or "", root)
        (evidence_dir / "collect.txt").write_text(out + "\n" + err, encoding="utf-8")
        if proc.returncode != 0 or _looks_like_env_failure(out, err, proc.returncode):
            (evidence_dir / "collect_failed.txt").write_text(out + "\n" + err, encoding="utf-8")
            return not_measured(
                "test_adequacy",
                null_reason="test_env_unavailable"
                if _looks_like_env_failure(out, err, proc.returncode)
                else "build_failed",
                raw_metrics={
                    **layout,
                    "suite": "collect_failed",
                    "collect_exit": proc.returncode,
                    "runner": runner,
                    "install": install_log,
                },
                evidence_refs=[
                    "test_adequacy/layout.json",
                    "test_adequacy/runner.json",
                    "test_adequacy/collect.txt",
                    "test_adequacy/collect_failed.txt",
                    "test_adequacy/install.json",
                ],
                analysis_scope=scope,
                tools=tools,
            )

        try:
            proc = run_scrubbed(
                [str(py), "-m", "pytest", "-q", "--maxfail=20"],
                cwd=root,
                env=env,
                timeout=run_timeout_s,
            )
        except subprocess.TimeoutExpired:
            (evidence_dir / "pytest.txt").write_text("TIMEOUT\n", encoding="utf-8")
            return not_measured(
                "test_adequacy",
                null_reason="timeout",
                raw_metrics={**layout, "suite": "run_timeout", "runner": runner},
                evidence_refs=[
                    "test_adequacy/layout.json",
                    "test_adequacy/runner.json",
                    "test_adequacy/pytest.txt",
                    "test_adequacy/install.json",
                ],
                analysis_scope=scope,
                tools=tools,
            )

        out = scrub_text_paths(proc.stdout or "", root)
        err = scrub_text_paths(proc.stderr or "", root)
        (evidence_dir / "pytest.txt").write_text(out + "\n" + err, encoding="utf-8")
        code = proc.returncode

        # B3: runner/missing-dep failures → NOT MEASURED, not subject FAIL.
        if code != 0 and _looks_like_env_failure(out, err, code):
            return not_measured(
                "test_adequacy",
                null_reason="test_env_unavailable",
                raw_metrics={
                    **layout,
                    "suite": "env_error",
                    "pytest_exit": code,
                    "runner": runner,
                    "install": install_log,
                },
                evidence_refs=[
                    "test_adequacy/layout.json",
                    "test_adequacy/runner.json",
                    "test_adequacy/collect.txt",
                    "test_adequacy/pytest.txt",
                    "test_adequacy/install.json",
                ],
                analysis_scope=scope,
                tools=tools,
            )

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
                "subject_code_executed": True,
                "env_scrubbed": True,
                "runner": runner,
                "install": {"installed": install_log.get("installed"), "ok": install_log.get("ok")},
            },
            evidence_refs=[
                "test_adequacy/layout.json",
                "test_adequacy/runner.json",
                "test_adequacy/collect.txt",
                "test_adequacy/pytest.txt",
                "test_adequacy/install.json",
            ],
            analysis_scope=scope,
            tools=tools,
            maturity="core",
        )
    finally:
        cleanup_dir(work)
