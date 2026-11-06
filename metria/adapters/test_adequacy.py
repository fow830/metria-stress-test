"""Test adequacy — static discovery always; pytest only in scrubbed isolated venv."""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from metria.discovery import TEST_DIR_NAMES, count_loc, existing_skip_dirs, find_test_layout
from metria.model import dimension, not_measured
from metria.sandbox import (
    cleanup_dir,
    create_subject_venv,
    install_subject_test_deps,
    run_scrubbed,
    scrub_env,
    scrub_text_paths,
)

_NO_MODULE = re.compile(r"No module named ['\"]?([A-Za-z0-9_.\-]+)['\"]?")
_FIXTURE_NOT_FOUND = re.compile(r"fixture ['\"][^'\"]+['\"] not found", re.I)
# Assertion only — do NOT match bare "FAILED" (that covers ImportError failures too).
_ASSERT_HINT = re.compile(r"(AssertionError|assert\s+.*(?:==|!=|<|>| is | in |True|False))", re.I)


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


def _subject_owned_names(root: Path) -> Set[str]:
    """Module/package names that resolve inside the subject tree (non-test)."""
    owned: Set[str] = set()
    for path in root.rglob("*.py"):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        parts = list(rel.with_suffix("").parts)
        if not parts:
            continue
        if parts[0] in TEST_DIR_NAMES or parts[0].startswith("test_"):
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        for i in range(len(parts)):
            owned.add(".".join(parts[: i + 1]))
            owned.add(parts[i])
    # Top-level packages/dirs
    try:
        for child in root.iterdir():
            if child.is_dir() and child.name not in TEST_DIR_NAMES and not child.name.startswith("."):
                if child.name not in {"dist", "build", "venv", ".venv", "docs", "examples"}:
                    owned.add(child.name)
            if child.is_file() and child.suffix == ".py" and not child.name.startswith("test_"):
                owned.add(child.stem)
    except OSError:
        pass
    return owned


def _module_resolves_in_subject(mod: str, root: Path, owned: Set[str]) -> bool:
    clean = mod.strip().strip(".")
    if not clean:
        return False
    if clean in owned or clean.split(".")[0] in owned:
        return True
    # File/dir resolve
    rel = Path(*clean.split("."))
    if (root / rel).is_dir() or (root / f"{rel}.py").is_file() or (root / rel / "__init__.py").is_file():
        return True
    top = clean.split(".", 1)[0]
    if (root / top).is_dir() or (root / f"{top}.py").is_file():
        return True
    return False


def _message_is_env_failure(msg: str, root: Path, owned: Set[str]) -> bool:
    """True only for runner/env gaps — never for subject-owned ImportErrors."""
    if _FIXTURE_NOT_FOUND.search(msg):
        return True
    m = _NO_MODULE.search(msg)
    if m:
        return not _module_resolves_in_subject(m.group(1), root, owned)
    # ImportError without a resolvable missing-module name: treat as subject unless
    # the message clearly names an external plugin (kept narrow — no whole-blob scan).
    if "ModuleNotFoundError" in msg or "ImportError" in msg:
        # cannot import name X from Y — if Y is subject-owned → subject
        fm = re.search(r"from ['\"]([^'\"]+)['\"]", msg)
        if fm and _module_resolves_in_subject(fm.group(1), root, owned):
            return False
        if fm and not _module_resolves_in_subject(fm.group(1), root, owned):
            return True
        return False
    return False


def _parse_junit(path: Path) -> List[Dict[str, str]]:
    """Return list of {kind, message, text} for failures/errors in junitxml."""
    if not path.is_file():
        return []
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return []
    items: List[Dict[str, str]] = []
    for case in tree.iter("testcase"):
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is None:
                continue
            items.append(
                {
                    "kind": tag,
                    "message": node.attrib.get("message") or "",
                    "text": (node.text or "") + "\n" + (node.attrib.get("message") or ""),
                }
            )
    return items


def _normalize_junit(text: str) -> str:
    """Strip wall-clock noise so evidence manifests are deterministic."""
    text = re.sub(r'\stimestamp="[^"]*"', ' timestamp="0"', text)
    text = re.sub(r'\stime="[^"]*"', ' time="0"', text)
    text = re.sub(r'\shostname="[^"]*"', ' hostname="host"', text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _classify_pytest_outcome(
    *,
    out: str,
    err: str,
    code: int,
    root: Path,
    junit_path: Optional[Path] = None,
) -> Tuple[str, List[str]]:
    """Classify a pytest run.

    Returns (kind, notes) where kind ∈ {ok, fail, env}.
    - ok: exit 0
    - fail: subject assertion / subject-owned import / other subject exception
    - env: only missing *external* deps / missing fixtures / pure runner gaps
    """
    if code == 0:
        return "ok", []
    owned = _subject_owned_names(root)
    notes: List[str] = []
    subject_hits = 0
    env_hits = 0

    junit_items = _parse_junit(junit_path) if junit_path is not None else []
    if junit_items:
        for item in junit_items:
            blob = f"{item.get('message', '')}\n{item.get('text', '')}"
            if _ASSERT_HINT.search(blob) and not _message_is_env_failure(blob, root, owned):
                subject_hits += 1
                notes.append("junit:assertion_or_subject")
                continue
            if _message_is_env_failure(blob, root, owned):
                env_hits += 1
                notes.append("junit:env")
            else:
                subject_hits += 1
                notes.append("junit:subject")
        if subject_hits > 0:
            return "fail", notes
        if env_hits > 0:
            return "env", notes

    blob = f"{out}\n{err}"
    # Assertion / FAILED lines → subject, even if ImportError appears elsewhere in the blob.
    if _ASSERT_HINT.search(blob):
        return "fail", notes + ["text:assertion_or_failed"]

    # Per-line / per-message env check — never "ImportError anywhere ⇒ env".
    env_only = False
    for m in _NO_MODULE.finditer(blob):
        if _module_resolves_in_subject(m.group(1), root, owned):
            return "fail", notes + [f"text:subject_module:{m.group(1)}"]
        env_only = True
    if _FIXTURE_NOT_FOUND.search(blob):
        env_only = True
    if env_only and subject_hits == 0:
        return "env", notes + ["text:external_import_or_fixture"]

    # Non-zero without env markers → subject failure (includes subject ImportError typos).
    if code != 0:
        return "fail", notes + [f"text:nonzero_exit:{code}"]
    return "ok", notes


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

    # Isolated venv + scrubbed env + install subject test deps.
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

        def _scrub(s: str) -> str:
            return scrub_text_paths(s, root, extra_roots=[work, home])

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

        out = _scrub(proc.stdout or "")
        err = _scrub(proc.stderr or "")
        (evidence_dir / "collect.txt").write_text(out + "\n" + err, encoding="utf-8")
        if proc.returncode != 0:
            kind, notes = _classify_pytest_outcome(
                out=out, err=err, code=proc.returncode, root=root
            )
            (evidence_dir / "collect_failed.txt").write_text(out + "\n" + err, encoding="utf-8")
            if kind == "env":
                return not_measured(
                    "test_adequacy",
                    null_reason="test_env_unavailable",
                    raw_metrics={
                        **layout,
                        "suite": "collect_failed",
                        "collect_exit": proc.returncode,
                        "classify_notes": notes,
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
            # Subject-owned collect failure (e.g. broken package import at collection) → FAIL
            return dimension(
                lens_id="test_adequacy",
                status="fail",
                confidence="medium",
                findings=[
                    {
                        "fingerprint": "adeq:collect_failed",
                        "title": f"pytest collection failed (exit {proc.returncode})",
                        "severity": "high",
                        "detectors": ["pytest"],
                        "confirmation": "unconfirmed",
                        "lens_routing_rule": "test_oracle_gap_to_test_adequacy",
                        "evidence_refs": ["test_adequacy/collect_failed.txt"],
                    }
                ],
                raw_metrics={
                    **layout,
                    "suite": "collect_failed",
                    "collect_exit": proc.returncode,
                    "classify_notes": notes,
                    "subject_code_executed": True,
                    "runner": runner,
                    "install": {"installed": install_log.get("installed"), "ok": install_log.get("ok")},
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
                maturity="core",
            )

        junit_path = evidence_dir / "junit.xml"
        try:
            proc = run_scrubbed(
                [
                    str(py),
                    "-m",
                    "pytest",
                    "-q",
                    "--maxfail=20",
                    f"--junitxml={junit_path}",
                ],
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

        out = _scrub(proc.stdout or "")
        err = _scrub(proc.stderr or "")
        (evidence_dir / "pytest.txt").write_text(out + "\n" + err, encoding="utf-8")
        # Classify from the raw junit, then rewrite a deterministic scrubbed copy.
        kind, notes = _classify_pytest_outcome(
            out=out, err=err, code=proc.returncode, root=root, junit_path=junit_path
        )
        if junit_path.is_file():
            raw_junit = junit_path.read_text(encoding="utf-8", errors="ignore")
            junit_path.write_text(_normalize_junit(_scrub(raw_junit)), encoding="utf-8")
        code = proc.returncode

        if kind == "env":
            env_finding = {
                "fingerprint": "adeq:env_gap",
                "title": "Test environment gap (external import/fixture); subject result not attributed",
                "severity": "medium",
                "detectors": ["pytest"],
                "confirmation": "unconfirmed",
                "lens_routing_rule": "test_oracle_gap_to_test_adequacy",
                "evidence_refs": ["test_adequacy/pytest.txt"],
            }
            nm = not_measured(
                "test_adequacy",
                null_reason="test_env_unavailable",
                raw_metrics={
                    **layout,
                    "suite": "env_error",
                    "pytest_exit": code,
                    "classify_notes": notes,
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
            nm["findings"] = [env_finding]
            nm["confidence"] = "low"
            return nm

        findings: List[Dict[str, Any]] = []
        if kind == "fail" or code != 0:
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

        evidence_refs = [
            "test_adequacy/layout.json",
            "test_adequacy/runner.json",
            "test_adequacy/collect.txt",
            "test_adequacy/pytest.txt",
            "test_adequacy/install.json",
        ]
        if junit_path.is_file():
            evidence_refs.append("test_adequacy/junit.xml")

        return dimension(
            lens_id="test_adequacy",
            status=status,
            confidence="medium",
            findings=findings,
            raw_metrics={
                **layout,
                "suite": suite,
                "pytest_exit": code,
                "classify_notes": notes,
                "strong_oracle": False,
                "subject_code_executed": True,
                "env_scrubbed": True,
                "runner": runner,
                "install": {"installed": install_log.get("installed"), "ok": install_log.get("ok")},
            },
            evidence_refs=evidence_refs,
            analysis_scope=scope,
            tools=tools,
            maturity="core",
        )
    finally:
        cleanup_dir(work)
