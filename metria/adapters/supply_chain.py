"""Supply-chain adapter — runtime inventory + OSV; fail-closed on lookup errors."""

from __future__ import annotations

import json
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from metria.discovery import (
    count_loc,
    existing_skip_dirs,
    find_lockfiles,
    find_sbom_practice,
    inventory_packages_with_meta,
)
from metria.model import dimension, not_measured
from metria.sandbox import (
    cleanup_dir,
    create_subject_venv,
    pip_list_versions,
    run_scrubbed,
    scrub_env,
)

OSV_URL = "https://api.osv.dev/v1/query"
# Exact pin only — no wildcards / trailing dots (N-r3-2: ==1.* must not become "1.").
VERSION_EQ_STRICT = re.compile(r"^==\s*([0-9]+(?:\.[0-9A-Za-z]+)*)$")
# First concrete lower-bound from a range specifier (query / resolve hint).
VERSION_LOWER = re.compile(r"(?:>=|>|~=|==)\s*([A-Za-z0-9_.+\-]+)")
CVSS_VECTOR = re.compile(r"^CVSS:3\.\d+/(.+)$")


def _osv_query(name: str, version: Optional[str], timeout: float = 12.0) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return (vulns, error_message). error_message set on network/parse failure."""
    payload: Dict[str, Any] = {"package": {"name": name, "ecosystem": "PyPI"}}
    if version:
        payload["version"] = version
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OSV_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    return body.get("vulns") or [], None


def _pinned_version(specifier: str) -> Optional[str]:
    """Exact ``==X.Y.Z`` only. Wildcards (``==1.*``) and truncated tokens are not pins."""
    s = (specifier or "").strip()
    if "*" in s or "," in s or s.endswith("."):
        return None
    m = VERSION_EQ_STRICT.match(s)
    if not m:
        return None
    ver = m.group(1)
    # Reject trailing empty segments / lone major with dangling junk already filtered.
    if ver.endswith(".") or not ver:
        return None
    return ver


def _range_lower_bound(specifier: str) -> Optional[str]:
    """Best-effort first version token from a range (N-r2-4). Never from wildcards."""
    s = (specifier or "").strip()
    if not s or "*" in s:
        return None
    pinned = _pinned_version(s)
    if pinned:
        return pinned
    m = VERSION_LOWER.search(s)
    if not m:
        return None
    ver = m.group(1)
    if "*" in ver or ver.endswith("."):
        return None
    return ver


def _norm_name(name: str) -> str:
    return name.lower().replace("_", "-")


def resolve_runtime_versions(
    root: Path,
    runtime: List[Dict[str, str]],
    *,
    allow_subject_install: bool = True,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Resolve concrete versions: pin → (optional) pip list after editable install.

    When ``allow_subject_install`` is False (``--no-tests``), never run
    ``pip install -e .`` — that executes subject setup.py (r3 B2 / r1 B1.2).
    """
    resolved: Dict[str, str] = {}
    meta: Dict[str, Any] = {
        "method": None,
        "errors": [],
        "venv_used": False,
        "allow_subject_install": allow_subject_install,
    }
    pending = []
    for pkg in runtime:
        key = _norm_name(pkg["name"])
        pin = _pinned_version(pkg.get("specifier") or "")
        if pin:
            resolved[key] = pin
            continue
        pending.append(pkg)

    if not pending:
        meta["method"] = "pinned"
        return resolved, meta

    if not allow_subject_install:
        # Static-only: leave unpinned unresolved → adapter_gap (no subject build).
        meta["method"] = "static_no_subject_exec"
        meta["errors"].append("version unresolved: subject code execution disabled")
        return resolved, meta

    # Try ephemeral venv + editable install → pip list (N-r2-4).
    work = Path(tempfile.mkdtemp(prefix="metria-sca-"))
    home = work / "home"
    home.mkdir()
    try:
        meta["venv_used"] = True
        env = scrub_env(home=home)
        try:
            py, pip = create_subject_venv(work)
        except Exception as exc:  # noqa: BLE001
            meta["errors"].append(f"venv:{exc}")
            meta["method"] = "unresolved"
            return resolved, meta
        env = scrub_env(home=home, extra={"VIRTUAL_ENV": str(work / "venv")})
        run_scrubbed(
            [str(pip), "install", "-q", "--upgrade", "pip"],
            cwd=root,
            env=env,
            timeout=120,
        )
        has_packaging = any((root / n).is_file() for n in ("pyproject.toml", "setup.py", "setup.cfg"))
        if has_packaging:
            proc = run_scrubbed(
                [str(pip), "install", "-q", "-e", "."],
                cwd=root,
                env=env,
                timeout=300,
            )
            if proc.returncode != 0:
                meta["errors"].append(f"pip_install:{(proc.stderr or '')[-300:]}")
        installed = pip_list_versions(pip, env)
        for pkg in pending:
            key = _norm_name(pkg["name"])
            if key in installed:
                resolved[key] = installed[key]
            else:
                lower = _range_lower_bound(pkg.get("specifier") or "")
                if lower:
                    resolved[key] = lower
                    meta.setdefault("range_fallback", []).append(pkg["name"])
        meta["method"] = "venv_pip_list" if installed else "range_or_none"
        _ = py
    finally:
        cleanup_dir(work)
    return resolved, meta


def _cvss3_base_from_vector(vector: str) -> Optional[float]:
    """Minimal CVSS v3 base-score estimate from a vector string."""
    m = CVSS_VECTOR.match(vector.strip())
    if not m:
        return None
    parts = {p.split(":")[0]: p.split(":")[1] for p in m.group(1).split("/") if ":" in p}
    av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}.get(parts.get("AV", ""), 0)
    ac = {"L": 0.77, "H": 0.44}.get(parts.get("AC", ""), 0)
    scope_changed = parts.get("S") == "C"
    pr_map = (
        {"N": 0.85, "L": 0.68, "H": 0.5}
        if scope_changed
        else {"N": 0.85, "L": 0.62, "H": 0.27}
    )
    pr = pr_map.get(parts.get("PR", ""), 0)
    ui = {"N": 0.85, "R": 0.62}.get(parts.get("UI", ""), 0)
    cia = {"N": 0.0, "L": 0.22, "H": 0.56}
    c = cia.get(parts.get("C", "N"), 0)
    i = cia.get(parts.get("I", "N"), 0)
    a = cia.get(parts.get("A", "N"), 0)
    exploitability = 8.22 * av * ac * pr * ui
    impact_sub = 1 - ((1 - c) * (1 - i) * (1 - a))
    if impact_sub <= 0:
        return 0.0
    if scope_changed:
        impact = 7.52 * (impact_sub - 0.029) - 3.25 * (impact_sub - 0.02) ** 15
        base = min(1.08 * (impact + exploitability), 10.0)
    else:
        impact = 6.42 * impact_sub
        base = min(impact + exploitability, 10.0)
    return round(max(base, 0.0), 1)


def _score_to_severity(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0:
        return "low"
    return "info"


def severity_from_osv(vuln: Dict[str, Any]) -> Optional[str]:
    """Prefer database_specific.severity / numeric CVSS; never default to high."""
    db = vuln.get("database_specific") or {}
    raw = str(db.get("severity") or "").strip().lower()
    if raw in {"critical", "high", "medium", "low", "info"}:
        return raw
    for key in ("cvss_score", "cvss"):
        val = db.get(key)
        if isinstance(val, (int, float)):
            return _score_to_severity(float(val))
        if isinstance(val, str):
            try:
                return _score_to_severity(float(val))
            except ValueError:
                pass
    for sev in vuln.get("severity") or []:
        score = sev.get("score")
        if isinstance(score, (int, float)):
            return _score_to_severity(float(score))
        if isinstance(score, str):
            s = score.strip()
            if s.upper() in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}:
                return s.lower()
            try:
                return _score_to_severity(float(s))
            except ValueError:
                pass
            base = _cvss3_base_from_vector(s)
            if base is not None:
                return _score_to_severity(base)
    return None


def query_osv_for_packages(
    packages: List[Dict[str, str]],
    *,
    resolved: Optional[Dict[str, str]] = None,
    max_packages: int = 40,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {
        "osv_queries": 0,
        "osv_vuln_hits": 0,
        "osv_skipped_unresolved": 0,
        "osv_errors": 0,
        "osv_error_samples": [],
    }
    resolved = resolved or {}
    for pkg in packages[:max_packages]:
        key = _norm_name(pkg["name"])
        version = resolved.get(key) or _pinned_version(pkg.get("specifier") or "")
        if not version:
            metrics["osv_skipped_unresolved"] += 1
            continue
        metrics["osv_queries"] += 1
        try:
            vulns, err = _osv_query(pkg["name"], version)
        except Exception as exc:  # noqa: BLE001 — count as lookup error
            metrics["osv_errors"] += 1
            if len(metrics["osv_error_samples"]) < 5:
                metrics["osv_error_samples"].append(f"{pkg['name']}: {exc}")
            continue
        if err:
            metrics["osv_errors"] += 1
            if len(metrics["osv_error_samples"]) < 5:
                metrics["osv_error_samples"].append(f"{pkg['name']}@{version}: {err}")
            continue
        if not vulns:
            continue
        metrics["osv_vuln_hits"] += len(vulns)
        for vuln in vulns[:5]:
            vid = vuln.get("id") or "UNKNOWN"
            summary = (vuln.get("summary") or vid).strip()
            severity = severity_from_osv(vuln)
            finding: Dict[str, Any] = {
                "fingerprint": f"sca:{vid}:{pkg['name']}@{version}",
                "title": f"{vid} in {pkg['name']}=={version}: {summary[:160]}",
                "detectors": ["osv"],
                "confirmation": "unconfirmed",
                "lens_routing_rule": "dependency_cve_to_supply_chain",
                "evidence_refs": ["supply_chain/osv.json"],
            }
            if severity:
                finding["severity"] = severity
            findings.append(finding)
    return findings, metrics


def run_supply_chain(
    root: Path,
    evidence_dir: Path,
    *,
    query_osv: bool = True,
    allow_subject_install: bool = True,
) -> Dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    loc = count_loc(root)
    packages, inv_meta = inventory_packages_with_meta(root)
    lockfiles = find_lockfiles(root)
    sbom_files = find_sbom_practice(root)
    runtime = [p for p in packages if p.get("class", "runtime") == "runtime"]
    non_runtime = [p for p in packages if p.get("class", "runtime") != "runtime"]
    excluded = existing_skip_dirs(root)

    inventory_doc = {
        "lockfiles": lockfiles,
        "packages": packages,
        "runtime_packages": runtime,
        "non_runtime_packages": non_runtime,
        "component_count": len(packages),
        "runtime_component_count": len(runtime),
        "inventory_meta": inv_meta,
    }
    (evidence_dir / "inventory.json").write_text(json.dumps(inventory_doc, indent=2), encoding="utf-8")

    sbom_doc = {"published_sbom_files": sbom_files}
    (evidence_dir / "sbom_practice.json").write_text(json.dumps(sbom_doc, indent=2), encoding="utf-8")

    scope = {
        "languages_analyzed": ["python"] if loc["py_files"] else [],
        "loc_analyzed": 0,
        "loc_total": loc["total_loc"],
        "excluded_paths": excluded,
        "exclusion_reasons": {d: "tooling_or_vcs" for d in excluded},
    }
    tools = [
        {"name": "metria-inventory", "version": "0.1.0", "ruleset_digest": None},
        {"name": "osv.dev", "version": "v1", "ruleset_digest": None},
    ]

    findings: List[Dict[str, Any]] = []
    osv_metrics: Dict[str, Any] = {
        "osv_queries": 0,
        "osv_vuln_hits": 0,
        "osv_skipped_unresolved": 0,
        "osv_errors": 0,
    }
    resolve_meta: Dict[str, Any] = {}
    resolved: Dict[str, str] = {}

    # N-r2-5: setup.py present but unreadable → inventory gap, not "no deps".
    setup_py_gap = bool(inv_meta.get("setup_py_present")) and bool(inv_meta.get("setup_py_error"))

    if query_osv and runtime:
        resolved, resolve_meta = resolve_runtime_versions(
            root, runtime, allow_subject_install=allow_subject_install
        )
        (evidence_dir / "resolved_versions.json").write_text(
            json.dumps({"resolved": resolved, "meta": resolve_meta}, indent=2), encoding="utf-8"
        )
        findings, osv_metrics = query_osv_for_packages(runtime, resolved=resolved)
        (evidence_dir / "osv.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
    else:
        (evidence_dir / "osv.json").write_text("[]\n", encoding="utf-8")
        (evidence_dir / "resolved_versions.json").write_text("{}\n", encoding="utf-8")

    # B2: any OSV lookup failure → NOT MEASURED (never PASS).
    if query_osv and int(osv_metrics.get("osv_errors") or 0) > 0:
        nm = not_measured(
            "supply_chain",
            null_reason="tool_error",
            raw_metrics={
                "components": len(packages),
                "runtime_components": len(runtime),
                "non_runtime_components": len(non_runtime),
                "lockfiles": lockfiles,
                "sbom_files": sbom_files,
                **osv_metrics,
                "resolve": resolve_meta,
            },
            evidence_refs=[
                "supply_chain/inventory.json",
                "supply_chain/sbom_practice.json",
                "supply_chain/osv.json",
                "supply_chain/resolved_versions.json",
            ],
            analysis_scope=scope,
            tools=tools,
        )
        nm["findings"] = [
            {
                "fingerprint": "sca:osv_incomplete",
                "title": (
                    f"CVE lookup incomplete: {osv_metrics['osv_errors']} error(s) "
                    f"across {osv_metrics.get('osv_queries', 0)} queries"
                ),
                "severity": "medium",
                "detectors": ["osv"],
                "confirmation": "unconfirmed",
                "lens_routing_rule": "dependency_cve_to_supply_chain",
                "evidence_refs": ["supply_chain/osv.json"],
            }
        ]
        return nm

    hard: List[str] = []
    for f in findings:
        if f.get("severity") == "critical":
            hard.append(f["title"])

    # N-r2-4: unresolved runtime → adapter_gap, not WARN for unpinned library practice.
    if query_osv and runtime:
        unresolved = [
            p
            for p in runtime
            if _norm_name(p["name"]) not in resolved and not _pinned_version(p.get("specifier") or "")
        ]
        for p in unresolved[:10]:
            findings.append(
                {
                    "fingerprint": f"sca:unresolved:{p['name'].lower()}",
                    "title": f"Could not resolve version for runtime dependency {p['name']} (adapter_gap)",
                    "severity": "low",
                    "detectors": ["inventory"],
                    "confirmation": "unconfirmed",
                    "lens_routing_rule": "dependency_cve_to_supply_chain",
                    "evidence_refs": ["supply_chain/resolved_versions.json"],
                }
            )

    if setup_py_gap and not packages and not lockfiles:
        return not_measured(
            "supply_chain",
            null_reason="adapter_gap",
            raw_metrics={
                "components": 0,
                "runtime_components": 0,
                "lockfiles": lockfiles,
                "sbom_files": sbom_files,
                "inventory_meta": inv_meta,
                **osv_metrics,
            },
            evidence_refs=[
                "supply_chain/inventory.json",
                "supply_chain/sbom_practice.json",
                "supply_chain/osv.json",
            ],
            analysis_scope=scope,
            tools=tools,
        )

    if not lockfiles and not packages:
        # N-12: library with no dependency surface → pass, not WARN.
        # Only when we actually read packaging sources (or none exist).
        status = "pass"
        findings.append(
            {
                "fingerprint": "sca:no-deps",
                "title": "No runtime dependencies declared (components=0)",
                "severity": "info",
                "detectors": ["inventory"],
                "confirmation": "unconfirmed",
                "lens_routing_rule": "dependency_cve_to_supply_chain",
                "evidence_refs": ["supply_chain/inventory.json"],
            }
        )
    elif hard:
        status = "fail"
    elif findings:
        status = "warn"
    else:
        status = "pass"

    sub_results = [
        {
            "id": "inventory",
            "status": "pass" if runtime or lockfiles or not packages else "pass",
            "raw_metrics": {
                "components": len(packages),
                "runtime_components": len(runtime),
                "lockfiles": lockfiles,
            },
        },
        {
            "id": "sbom_practice",
            "status": "pass" if sbom_files else "null",
            **(
                {
                    "null_reason": "not_declared_by_subject",
                    "null_owner": "subject",
                    "null_display": "UNDECLARED",
                }
                if not sbom_files
                else {}
            ),
            "raw_metrics": {"published_sbom": bool(sbom_files), "files": sbom_files},
        },
    ]

    evidence_refs = [
        "supply_chain/inventory.json",
        "supply_chain/sbom_practice.json",
        "supply_chain/osv.json",
        "supply_chain/resolved_versions.json",
    ]
    return dimension(
        lens_id="supply_chain",
        status=status,
        confidence="medium" if runtime else "low",
        findings=findings,
        hard_gate_failures=hard[:10],
        raw_metrics={
            "components": len(packages),
            "runtime_components": len(runtime),
            "non_runtime_components": len(non_runtime),
            "lockfiles": lockfiles,
            "sbom_files": sbom_files,
            "resolved_versions": len(resolved),
            "inventory_meta": inv_meta,
            **osv_metrics,
        },
        evidence_refs=evidence_refs,
        analysis_scope=scope,
        tools=tools,
        sub_results=sub_results,
        maturity="core",
    )
