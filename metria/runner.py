"""Assemble a StressRun for a local checkout at a fixed commit."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from metria.adapters import structural, supply_chain, test_adequacy
from metria.discovery import (
    discover,
    find_architecture_rules,
    find_normative_artifacts,
    count_loc,
    existing_skip_dirs,
)
from metria.evidence import write_evidence_pack, write_json
from metria.model import aggregate_overall, top_risks_from_lenses, undeclared, utc_now
from metria.render import render_report

SCHEMA_VERSION = "0.1"


def load_policy(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def policy_digest(policy: Dict[str, Any]) -> str:
    blob = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def git_commit_sha(root: Path) -> str:
    return (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True)
        .strip()
        .lower()
    )


def git_tree_digest(root: Path) -> str:
    # N-11: honest label of git tree id (not a content hash of the tree object bytes).
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=str(root), text=True
    ).strip()
    return f"git_tree:{tree}"


def runner_environment() -> Dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "python_full": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "metria_version": "0.1.0",
    }


def _scope(root: Path) -> Dict[str, Any]:
    loc = count_loc(root)
    excluded = existing_skip_dirs(root)
    return {
        "languages_analyzed": ["python"] if loc["py_files"] else [],
        "loc_analyzed": loc["py_loc"],
        "loc_total": loc["total_loc"],
        "excluded_paths": excluded,
        "exclusion_reasons": {d: "tooling_or_vcs" for d in excluded},
    }


def _controls_undeclared(root: Path) -> Dict[str, Any]:
    return undeclared(
        "controls",
        raw_metrics={"asvs_scope_provided": False},
        evidence_refs=["subject_manifest.json"],
        analysis_scope=_scope(root),
    )


def _architectural_undeclared(root: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    arch = manifest.get("architecture") or find_architecture_rules(root)
    if not any(arch.values()):
        return undeclared(
            "architectural",
            raw_metrics={"checked": ["import-linter", "ArchUnit", "Sonar architecture"], **arch},
            evidence_refs=["subject_manifest.json"],
            analysis_scope=_scope(root),
        )
    from metria.model import not_measured

    return not_measured(
        "architectural",
        null_reason="adapter_gap",
        raw_metrics={"checked": ["import-linter", "ArchUnit", "Sonar architecture"], **arch},
        evidence_refs=["subject_manifest.json"],
        analysis_scope=_scope(root),
    )


def _normative_undeclared(root: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    normative = manifest.get("normative") or find_normative_artifacts(root)
    return undeclared(
        "normative",
        maturity="experimental",
        raw_metrics=normative,
        evidence_refs=["subject_manifest.json"],
        analysis_scope=_scope(root),
    )


def run_subject(
    root: Path,
    *,
    repo_url: str,
    policy: Dict[str, Any],
    out_dir: Path,
    commit_sha: Optional[str] = None,
    query_osv: bool = True,
    run_tests: bool = True,
) -> Dict[str, Any]:
    started = time.monotonic()
    started_at = utc_now()
    root = root.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence_root = out_dir / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)

    sha = (commit_sha or git_commit_sha(root)).lower()
    tree = git_tree_digest(root)
    manifest = discover(root)
    env_info = runner_environment()
    write_json(evidence_root / "subject_manifest.json", manifest)
    write_json(evidence_root / "runner_environment.json", env_info)

    structural_lens = structural.run_structural(root, evidence_root / "structural")
    # r3 B2: never pip-install the subject when --no-tests (setup.py would run).
    supply_lens = supply_chain.run_supply_chain(
        root,
        evidence_root / "supply_chain",
        query_osv=query_osv,
        allow_subject_install=run_tests,
    )
    adequacy_lens = test_adequacy.run_test_adequacy(
        root,
        evidence_root / "test_adequacy",
        run_tests=run_tests,
        collect_timeout_s=int(policy.get("test_collect_timeout_s", 60)),
        run_timeout_s=int(policy.get("test_run_timeout_s", 180)),
    )
    lenses = {
        "structural": structural_lens,
        "supply_chain": supply_lens,
        "controls": _controls_undeclared(root),
        "test_adequacy": adequacy_lens,
        "architectural": _architectural_undeclared(root, manifest),
        "normative": _normative_undeclared(root, manifest),
    }

    overall = aggregate_overall(lenses)
    risks = top_risks_from_lenses(lenses)

    zip_path = out_dir / "evidence.zip"
    zip_sha, manifest_sha = write_evidence_pack(evidence_root, zip_path)

    finished_at = utc_now()
    used = int(time.monotonic() - started)
    # B4: record runner profile; schema allows null runner_image_digest — put digest-like tag here.
    runner_tag = (
        f"local:{env_info['system']}-{env_info['machine']}/cpython-{env_info['python']}"
    )
    run: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"run_{uuid.uuid4().hex[:12]}",
        "subject": {
            "repo_url": repo_url,
            "commit_sha": sha if len(sha) <= 40 else sha[:40],
            "tree_digest": tree,
        },
        "runner_image_digest": runner_tag,
        "policy_digest": policy_digest(policy),
        "started_at": started_at,
        "finished_at": finished_at,
        "budget": {
            "wall_clock_s": int(policy.get("wall_clock_s", 1800)),
            "used_s": used,
        },
        "overall": overall,
        "lenses": lenses,
        "top_risks": risks,
        "evidence_pack": {
            "uri": "evidence.zip",
            "sha256": zip_sha,
            "manifest_sha256": manifest_sha,
        },
    }

    for banned in ("ideal_score", "idealnost", "overall_score", "risk_score", "grade", "band"):
        run.pop(banned, None)

    write_json(out_dir / "stress-run.json", run)
    (out_dir / "report.txt").write_text(render_report(run), encoding="utf-8")
    return run
