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

_MISSING_MOD = re.compile(
    r"(?:No module named\s+|ModuleNotFoundError:\s*(?:No module named\s+)?)['\"]?"
    r"([A-Za-z0-9_][A-Za-z0-9_\.]*)",
    re.I,
)
_IMPORT_ERR_LINE = re.compile(r"^\s*(?:E\s+)?(?:ImportError|ModuleNotFoundError)\b", re.M | re.I)
_FIXTURE_MISSING = re.compile(r"fixture ['\"][^'\"]+['\"] not found", re.I)
_FAILED_LINE = re.compile(r"^FAILED\b", re.M)
_ERROR_LINE = re.compile(r"^ERROR\b", re.M)


def _normalize_junit(text: str, root: Path, *extra_roots: Path) -> str:
    """Scrub paths and drop volatile junit attrs so evidence manifests stay stable."""
    text = scrub_text_paths(text, root, *extra_roots)
    text = re.sub(r'\s+timestamp="[^"]*"', "", text)
    text = re.sub(r'\s+hostname="[^"]*"', "", text)
    text = re.sub(r'\s+time="[^"]*"', ' time="0"', text)
    return text


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


_SKIP_TOP = frozenset(
    {"tests", "test", "docs", "doc", "examples", "example", "build", "dist", "venv", ".venv"}
)


def _collect_pkg_names_under(base: Path) -> Set[str]:
    """Importable top-level names under ``base`` (packages with __init__ or *.py modules)."""
    names: Set[str] = set()
    if not base.is_dir():
        return names
    try:
        for p in base.iterdir():
            if p.name.startswith(".") or p.name in _SKIP_TOP:
                continue
            if p.is_dir() and ((p / "__init__.py").exists() or any(p.glob("*.py"))):
                names.add(p.name)
            elif p.suffix == ".py" and p.name not in {"setup.py", "conftest.py"}:
                names.add(p.stem)
    except OSError:
        pass
    return names


def _subject_module_names(root: Path) -> Set[str]:
    """Import names that belong to the subject tree (flat + src-layout, r4 B1).

    Prefer filesystem packages over distribution ``name =`` (Pillow≠PIL).
    """
    names = _collect_pkg_names_under(root)
    # src-layout: packages live under src/<pkg>/ (r4 B1 — independent of dist name).
    names |= _collect_pkg_names_under(root / "src")
    # lib/ is another common packaging root.
    names |= _collect_pkg_names_under(root / "lib")
    # Distribution name is a weak hint only when no filesystem packages found.
    if not names:
        for meta in ("pyproject.toml", "setup.cfg"):
            path = root / meta
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = re.search(r"(?m)^\s*name\s*=\s*[\"']?([A-Za-z0-9][A-Za-z0-9._\-]*)", text)
            if m:
                names.add(m.group(1).split(".")[0].replace("-", "_"))
    return names


def _missing_modules(blob: str) -> List[str]:
    return [m.group(1) for m in _MISSING_MOD.finditer(blob or "")]


def _module_belongs_to_subject(mod: str, subject_names: Set[str], root: Path) -> bool:
    top = (mod or "").split(".")[0]
    if not top:
        return False
    if top in subject_names:
        return True
    # Flat or src-layout path existence (most reliable for r4 B1).
    for base in (root, root / "src"):
        if (base / top).is_dir() or (base / f"{top}.py").is_file():
            return True
    return False


def _junit_classify(
    junit_path: Path, subject_names: Set[str], root: Path
) -> Tuple[bool, bool, List[str]]:
    """Per-testcase attribution → (has_subject_failure, has_env_error, notes).

    r3 B3: ModuleNotFoundError/ImportError inside a ``<failure>`` is env when the
    missing module is not part of the subject tree (six/_dbm case).
    """
    has_subject = False
    has_env = False
    notes: List[str] = []
    try:
        tree = ET.parse(str(junit_path))
    except (ET.ParseError, OSError):
        return False, False, ["junit_parse_error"]
    subject_assert_types = {
        "AssertionError",
        "Failed",
        "pytest.fail",
    }
    for case in tree.iter("testcase"):
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is None:
                continue
            msg = f"{node.get('message') or ''}\n{node.text or ''}"
            ftype = (node.get("type") or "").strip()
            notes.append(f"{tag}:{ftype or 'unknown'}")
            # Import / ModuleNotFound — attribute by module ownership (r3 B3).
            is_import = (
                "ImportError" in ftype
                or "ModuleNotFoundError" in ftype
                or "ImportError" in msg
                or "ModuleNotFoundError" in msg
            )
            if is_import:
                mods = _missing_modules(msg)
                if not mods:
                    has_subject = True
                    continue
                for mod in mods:
                    if _module_belongs_to_subject(mod, subject_names, root):
                        has_subject = True
                    else:
                        has_env = True
                continue
            if _FIXTURE_MISSING.search(msg):
                has_env = True
                continue
            if ftype in subject_assert_types or "AssertionError" in msg:
                has_subject = True
                continue
            # Other exception types in a test body → subject defect by default.
            has_subject = True
    return has_subject, has_env, notes


def classify_pytest_result(
    out: str,
    err: str,
    code: int,
    root: Path,
    *,
    phase: str = "run",
    junit_path: Optional[Path] = None,
) -> str:
    """Return ``pass`` | ``fail`` | ``env``.

    Env only when attribution is reliable (third-party ModuleNotFound / missing fixture /
    collection error for non-subject modules). Subject ImportError and AssertionError → fail.
    Do not treat bare ``FAILED`` lines as subject (r3 B3) — prefer junit per-case.
    """
    if code == 0:
        return "pass"

    blob = f"{out}\n{err}"
    subject_names = _subject_module_names(root)
    has_subject = "AssertionError" in blob
    has_env = False
    used_junit = False

    if junit_path is not None and junit_path.is_file():
        j_subj, j_env, _notes = _junit_classify(junit_path, subject_names, root)
        has_subject = has_subject or j_subj
        has_env = has_env or j_env
        used_junit = True

    for mod in _missing_modules(blob):
        if _module_belongs_to_subject(mod, subject_names, root):
            has_subject = True
        else:
            has_env = True

    if _FIXTURE_MISSING.search(blob):
        has_env = True

    # Without junit, a FAILED line + no env attribution → subject; with junit, ignore FAILED.
    if not used_junit and _FAILED_LINE.search(blob) and not has_env:
        has_subject = True

    # Mixed: any subject failure wins → fail (r2 B1).
    if has_subject:
        return "fail"

    # Only env cases → NOT MEASURED (r3 B3).
    if has_env:
        return "env"

    # Collection with no subject attribution: pytest usage/internal codes → env.
    if phase == "collect" and code in {2, 3, 4} and not _IMPORT_ERR_LINE.search(blob):
        return "env"

    # Everything else with non-zero exit → subject fail (never silent NOT MEASURED).
    return "fail"


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

        out = scrub_text_paths(proc.stdout or "", root, work, home, evidence_dir)
        err = scrub_text_paths(proc.stderr or "", root, work, home, evidence_dir)
        (evidence_dir / "collect.txt").write_text(out + "\n" + err, encoding="utf-8")
        if proc.returncode != 0:
            kind = classify_pytest_result(out, err, proc.returncode, root, phase="collect")
            (evidence_dir / "collect_failed.txt").write_text(out + "\n" + err, encoding="utf-8")
            if kind == "env":
                return not_measured(
                    "test_adequacy",
                    null_reason="test_env_unavailable",
                    raw_metrics={
                        **layout,
                        "suite": "collect_failed",
                        "collect_exit": proc.returncode,
                        "runner": runner,
                        "install": install_log,
                        "classify": kind,
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
            # Subject defect visible at collection → fail (not NOT MEASURED).
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
                    "strong_oracle": False,
                    "subject_code_executed": True,
                    "runner": runner,
                    "install": {"installed": install_log.get("installed"), "ok": install_log.get("ok")},
                    "classify": kind,
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

        junit_path = (evidence_dir / "junit.xml").resolve()
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

        out = scrub_text_paths(proc.stdout or "", root, work, home, evidence_dir)
        err = scrub_text_paths(proc.stderr or "", root, work, home, evidence_dir)
        # Drop the junit path line entirely — it embeds the evidence dir name.
        out = re.sub(r"^- generated xml file:.*$", "- generated xml file: <junit>", out, flags=re.M)
        (evidence_dir / "pytest.txt").write_text(out + "\n" + err, encoding="utf-8")
        # Classify from raw junit before scrubbing volatile attrs (timestamp/hostname).
        code = proc.returncode
        kind = classify_pytest_result(
            out, err, code, root, phase="run", junit_path=junit_path if junit_path.is_file() else None
        )
        if junit_path.is_file():
            junit_path.write_text(
                _normalize_junit(
                    junit_path.read_text(encoding="utf-8", errors="ignore"),
                    root,
                    work,
                    home,
                    evidence_dir,
                ),
                encoding="utf-8",
            )

        if kind == "env":
            return not_measured(
                "test_adequacy",
                null_reason="test_env_unavailable",
                raw_metrics={
                    **layout,
                    "suite": "env_error",
                    "pytest_exit": code,
                    "runner": runner,
                    "install": install_log,
                    "classify": kind,
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
                "strong_oracle": False,
                "subject_code_executed": True,
                "env_scrubbed": True,
                "runner": runner,
                "install": {"installed": install_log.get("installed"), "ok": install_log.get("ok")},
                "classify": kind,
            },
            evidence_refs=evidence_refs,
            analysis_scope=scope,
            tools=tools,
            maturity="core",
        )
    finally:
        cleanup_dir(work)
