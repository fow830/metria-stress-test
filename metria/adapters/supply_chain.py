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
    inventory_packages,
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
VERSION_EQ = re.compile(r"^==\s*([A-Za-z0-9_.+\-]+)$")
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
    m = VERSION_EQ.match((specifier or "").strip())
    return m.group(1) if m else None


def _norm_name(name: str) -> str:
    return name.lower().replace("_", "-")


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
    for key in ("cvss_score", "cvss", "github_reviewed_at"):
        if key == "github_reviewed_at":
            continue
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


def _resolve_runtime_versions(
    root: Path, runtime: List[Dict[str, str]]
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Best-effort: install subject into a throwaway venv and read pip list.

    Returns (name→version, meta). On failure meta explains adapter_gap; no WARN for ranges.
    """
    meta: Dict[str, Any] = {"resolved": False, "error": None, "attempted": False}
    need = [p for p in runtime if not _pinned_version(p.get("specifier") or "")]
    if not need:
        # Still useful to resolve pins from env, but not required.
        pinned = {
            _norm_name(p["name"]): _pinned_version(p.get("specifier") or "") or ""
            for p in runtime
            if _pinned_version(p.get("specifier") or "")
        }
        return {k: v for k, v in pinned.items() if v}, meta

    meta["attempted"] = True
    work = Path(tempfile.mkdtemp(prefix="metria-sca-"))
    home = work / "home"
    home.mkdir()
    try:
        env = scrub_env(home=home)
        try:
            py, pip = create_subject_venv(work)
        except Exception as exc:  # noqa: BLE001
            meta["error"] = f"venv: {exc}"
            return {}, meta
        env = scrub_env(home=home, extra={"VIRTUAL_ENV": str(work / "venv")})
        # Prefer editable / requirements so ranges resolve to concrete versions.
        args_list = []
        if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file() or (root / "setup.cfg").is_file():
            args_list.append(["-e", "."])
        elif (root / "requirements.txt").is_file():
            args_list.append(["-r", "requirements.txt"])
        else:
            # Install declared runtime pins/ranges directly.
            args_list.append([f"{p['name']}{p.get('specifier') or ''}" for p in runtime[:40]])
        ok = False
        for args in args_list:
            if not args:
                continue
            proc = run_scrubbed(
                [str(pip), "install", "-q", *args],
                cwd=root,
                env=env,
                timeout=300,
            )
            if proc.returncode == 0:
                ok = True
                break
            meta["error"] = (proc.stderr or proc.stdout or "")[-300:]
        if not ok:
            return {}, meta
        versions = pip_list_versions(pip, env)
        meta["resolved"] = bool(versions)
        return versions, meta
    finally:
        cleanup_dir(work)


def query_osv_for_packages(
    packages: List[Dict[str, str]],
    *,
    resolved_versions: Optional[Dict[str, str]] = None,
    max_packages: int = 40,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {
        "osv_queries": 0,
        "osv_vuln_hits": 0,
        "osv_skipped_unpinned": 0,
        "osv_adapter_gaps": 0,
        "osv_errors": 0,
        "osv_error_samples": [],
    }
    resolved_versions = resolved_versions or {}
    for pkg in packages[:max_packages]:
        name = pkg["name"]
        version = _pinned_version(pkg.get("specifier") or "")
        if not version:
            version = resolved_versions.get(_norm_name(name))
        if not version:
            metrics["osv_skipped_unpinned"] += 1
            metrics["osv_adapter_gaps"] += 1
            findings.append(
                {
                    "fingerprint": f"sca:adapter_gap:unresolved:{name.lower()}",
                    "title": (
                        f"Could not resolve installed version for {name}; "
                        "CVE lookup skipped (adapter_gap — range specs are fine)"
                    ),
                    "severity": "info",
                    "detectors": ["inventory"],
                    "confirmation": "unconfirmed",
                    "lens_routing_rule": "dependency_cve_to_supply_chain",
                    "evidence_refs": ["supply_chain/inventory.json"],
                }
            )
            continue
        metrics["osv_queries"] += 1
        try:
            vulns, err = _osv_query(name, version)
        except Exception as exc:  # noqa: BLE001 — count as lookup error
            metrics["osv_errors"] += 1
            if len(metrics["osv_error_samples"]) < 5:
                metrics["osv_error_samples"].append(f"{name}: {exc}")
            continue
        if err:
            metrics["osv_errors"] += 1
            if len(metrics["osv_error_samples"]) < 5:
                metrics["osv_error_samples"].append(f"{name}@{version}: {err}")
            continue
        if not vulns:
            continue
        metrics["osv_vuln_hits"] += len(vulns)
        for vuln in vulns[:5]:
            vid = vuln.get("id") or "UNKNOWN"
            summary = (vuln.get("summary") or vid).strip()
            severity = severity_from_osv(vuln)
            finding: Dict[str, Any] = {
                "fingerprint": f"sca:{vid}:{name}@{version}",
                "title": f"{vid} in {name}=={version}: {summary[:160]}",
                "detectors": ["osv"],
                "confirmation": "unconfirmed",
                "lens_routing_rule": "dependency_cve_to_supply_chain",
                "evidence_refs": ["supply_chain/osv.json"],
            }
            if severity:
                finding["severity"] = severity
            findings.append(finding)
    return findings, metrics


def run_supply_chain(root: Path, evidence_dir: Path, *, query_osv: bool = True) -> Dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    loc = count_loc(root)
    packages = inventory_packages(root)
    lockfiles = find_lockfiles(root)
    sbom_files = find_sbom_practice(root)
    gaps = [p for p in packages if p.get("class") == "adapter_gap"]
    runtime = [p for p in packages if p.get("class", "runtime") == "runtime"]
    non_runtime = [p for p in packages if p.get("class", "runtime") not in {"runtime", "adapter_gap"}]
    excluded = existing_skip_dirs(root)

    inventory_doc = {
        "lockfiles": lockfiles,
        "packages": packages,
        "runtime_packages": runtime,
        "non_runtime_packages": non_runtime,
        "adapter_gaps": gaps,
        "component_count": len(runtime) + len(non_runtime),
        "runtime_component_count": len(runtime),
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
        "osv_skipped_unpinned": 0,
        "osv_adapter_gaps": 0,
        "osv_errors": 0,
    }

    # N-r2-5: setup.py present but unparsed → NOT MEASURED inventory, never "components=0".
    if gaps and not runtime and not non_runtime:
        nm = not_measured(
            "supply_chain",
            null_reason="adapter_gap",
            raw_metrics={
                "components": 0,
                "runtime_components": 0,
                "non_runtime_components": 0,
                "lockfiles": lockfiles,
                "sbom_files": sbom_files,
                "adapter_gaps": [g.get("source") for g in gaps],
            },
            evidence_refs=[
                "supply_chain/inventory.json",
                "supply_chain/sbom_practice.json",
            ],
            analysis_scope=scope,
            tools=tools,
        )
        nm["findings"] = [
            {
                "fingerprint": f"sca:adapter_gap:parse:{g.get('source', 'unknown')}",
                "title": (
                    f"Dependency metadata at {g.get('source')} could not be parsed statically "
                    "(adapter_gap); inventory incomplete"
                ),
                "severity": "medium",
                "detectors": ["inventory"],
                "confirmation": "unconfirmed",
                "lens_routing_rule": "dependency_cve_to_supply_chain",
                "evidence_refs": ["supply_chain/inventory.json"],
            }
            for g in gaps
        ]
        return nm

    resolved: Dict[str, str] = {}
    resolve_meta: Dict[str, Any] = {}
    if query_osv and runtime:
        # Seed resolved map with pins; attempt venv resolve for ranges (N-r2-4).
        for p in runtime:
            pin = _pinned_version(p.get("specifier") or "")
            if pin:
                resolved[_norm_name(p["name"])] = pin
        need_resolve = [p for p in runtime if not _pinned_version(p.get("specifier") or "")]
        if need_resolve:
            got, resolve_meta = _resolve_runtime_versions(root, runtime)
            resolved.update(got)
        findings, osv_metrics = query_osv_for_packages(runtime, resolved_versions=resolved)
        (evidence_dir / "osv.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
        (evidence_dir / "resolved_versions.json").write_text(
            json.dumps({"versions": resolved, "meta": resolve_meta}, indent=2),
            encoding="utf-8",
        )
    else:
        (evidence_dir / "osv.json").write_text("[]\n", encoding="utf-8")

    # B2: any OSV lookup failure → NOT MEASURED (never PASS).
    if query_osv and int(osv_metrics.get("osv_errors") or 0) > 0:
        nm = not_measured(
            "supply_chain",
            null_reason="tool_error",
            raw_metrics={
                "components": len(runtime) + len(non_runtime),
                "runtime_components": len(runtime),
                "non_runtime_components": len(non_runtime),
                "lockfiles": lockfiles,
                "sbom_files": sbom_files,
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

    # Note parse gaps alongside a partial inventory (do not null the whole lens).
    for g in gaps:
        findings.append(
            {
                "fingerprint": f"sca:adapter_gap:parse:{g.get('source', 'unknown')}",
                "title": f"Partial parse gap for {g.get('source')} (adapter_gap)",
                "severity": "info",
                "detectors": ["inventory"],
                "confirmation": "unconfirmed",
                "lens_routing_rule": "dependency_cve_to_supply_chain",
                "evidence_refs": ["supply_chain/inventory.json"],
            }
        )

    actionable = [
        f
        for f in findings
        if f.get("severity") not in {None, "info"}
        and not str(f.get("fingerprint") or "").startswith("sca:adapter_gap:")
    ]

    if not lockfiles and not runtime and not non_runtime and not gaps:
        # N-12: library with no dependency surface → pass, not WARN.
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
    elif actionable:
        status = "warn"
    else:
        status = "pass"

    sub_results = [
        {
            "id": "inventory",
            "status": "pass",
            "raw_metrics": {
                "components": len(runtime) + len(non_runtime),
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
    ]
    if (evidence_dir / "resolved_versions.json").is_file():
        evidence_refs.append("supply_chain/resolved_versions.json")
    return dimension(
        lens_id="supply_chain",
        status=status,
        confidence="medium" if runtime else "low",
        findings=findings,
        hard_gate_failures=hard[:10],
        raw_metrics={
            "components": len(runtime) + len(non_runtime),
            "runtime_components": len(runtime),
            "non_runtime_components": len(non_runtime),
            "lockfiles": lockfiles,
            "sbom_files": sbom_files,
            "resolve_meta": resolve_meta,
            **osv_metrics,
        },
        evidence_refs=evidence_refs,
        analysis_scope=scope,
        tools=tools,
        sub_results=sub_results,
        maturity="core",
    )
