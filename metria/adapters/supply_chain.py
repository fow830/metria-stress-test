"""Supply-chain adapter — inventory from lockfiles + OSV queries; SBOM practice separate."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from metria.discovery import count_loc, inventory_packages, find_lockfiles, find_sbom_practice, SKIP_DIRS
from metria.model import dimension

OSV_URL = "https://api.osv.dev/v1/query"
VERSION_EQ = re.compile(r"^==\s*([A-Za-z0-9_.+\-]+)$")


def _osv_query(name: str, version: Optional[str], timeout: float = 12.0) -> List[Dict[str, Any]]:
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
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return []
    return body.get("vulns") or []


def _pinned_version(specifier: str) -> Optional[str]:
    m = VERSION_EQ.match((specifier or "").strip())
    return m.group(1) if m else None


def query_osv_for_packages(
    packages: List[Dict[str, str]], *, max_packages: int = 40
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {
        "osv_queries": 0,
        "osv_vuln_hits": 0,
        "osv_skipped_unpinned": 0,
        "osv_errors": 0,
    }
    for pkg in packages[:max_packages]:
        version = _pinned_version(pkg.get("specifier") or "")
        if not version:
            metrics["osv_skipped_unpinned"] += 1
            continue
        metrics["osv_queries"] += 1
        try:
            vulns = _osv_query(pkg["name"], version)
        except Exception:
            metrics["osv_errors"] += 1
            continue
        if not vulns:
            continue
        metrics["osv_vuln_hits"] += len(vulns)
        for vuln in vulns[:5]:
            vid = vuln.get("id") or "UNKNOWN"
            summary = (vuln.get("summary") or vid).strip()
            severity = "high"
            # Prefer explicit severity if present
            for sev in vuln.get("severity") or []:
                score = str(sev.get("score") or "")
                if score.startswith("CRITICAL") or "9." in score or "10." in score:
                    severity = "critical"
                    break
                if score.startswith("HIGH") or score[:1] in {"7", "8"}:
                    severity = "high"
            findings.append(
                {
                    "fingerprint": f"sca:{vid}:{pkg['name']}@{version}",
                    "title": f"{vid} in {pkg['name']}=={version}: {summary[:160]}",
                    "severity": severity,
                    "detectors": ["osv"],
                    "confirmation": "unconfirmed",
                    "lens_routing_rule": "dependency_cve_to_supply_chain",
                    "evidence_refs": ["supply_chain/osv.json"],
                }
            )
    return findings, metrics


def run_supply_chain(root: Path, evidence_dir: Path, *, query_osv: bool = True) -> Dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    loc = count_loc(root)
    packages = inventory_packages(root)
    lockfiles = find_lockfiles(root)
    sbom_files = find_sbom_practice(root)

    inventory_doc = {
        "lockfiles": lockfiles,
        "packages": packages,
        "component_count": len(packages),
    }
    (evidence_dir / "inventory.json").write_text(json.dumps(inventory_doc, indent=2), encoding="utf-8")

    sbom_doc = {"published_sbom_files": sbom_files}
    (evidence_dir / "sbom_practice.json").write_text(json.dumps(sbom_doc, indent=2), encoding="utf-8")

    findings: List[Dict[str, Any]] = []
    osv_metrics: Dict[str, Any] = {}
    if query_osv and packages:
        findings, osv_metrics = query_osv_for_packages(packages)
        (evidence_dir / "osv.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
    else:
        (evidence_dir / "osv.json").write_text("[]\n", encoding="utf-8")

    hard: List[str] = []
    for f in findings:
        if f.get("severity") == "critical":
            hard.append(f["title"])

    if not lockfiles and not packages:
        # no dependency surface declared
        status = "warn"
        findings.append(
            {
                "fingerprint": "sca:no-lockfile",
                "title": "No lockfile/requirements inventory found",
                "severity": "medium",
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
            "status": "pass" if packages or lockfiles else "warn",
            "raw_metrics": {"components": len(packages), "lockfiles": lockfiles},
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
    return dimension(
        lens_id="supply_chain",
        status=status,
        confidence="medium" if packages else "low",
        findings=findings,
        hard_gate_failures=hard[:10],
        raw_metrics={
            "components": len(packages),
            "lockfiles": lockfiles,
            "sbom_files": sbom_files,
            **osv_metrics,
        },
        evidence_refs=evidence_refs,
        analysis_scope={
            "languages_analyzed": ["python"] if loc["py_files"] else [],
            "loc_analyzed": loc["py_loc"],
            "loc_total": loc["total_loc"],
            "excluded_paths": sorted(SKIP_DIRS),
            "exclusion_reasons": {d: "tooling_or_vcs" for d in sorted(SKIP_DIRS)},
        },
        tools=[
            {"name": "metria-inventory", "version": "0.1.0", "ruleset_digest": None},
            {"name": "osv.dev", "version": "v1", "ruleset_digest": None},
        ],
        sub_results=sub_results,
        maturity="core",
    )
