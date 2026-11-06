"""Helpers that always emit schema-valid, score-null DimensionResults."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def empty_scope(
    languages: Optional[List[str]] = None,
    loc_analyzed: int = 0,
    loc_total: int = 0,
    excluded_paths: Optional[List[str]] = None,
    exclusion_reasons: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return {
        "languages_analyzed": languages or [],
        "loc_analyzed": loc_analyzed,
        "loc_total": loc_total,
        "excluded_paths": excluded_paths or [],
        "exclusion_reasons": exclusion_reasons or {},
    }


def dimension(
    *,
    lens_id: str,
    status: str,
    confidence: str = "low",
    findings: Optional[List[Dict[str, Any]]] = None,
    raw_metrics: Optional[Dict[str, Any]] = None,
    evidence_refs: Optional[List[str]] = None,
    analysis_scope: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    hard_gate_failures: Optional[List[str]] = None,
    suppressions_applied: Optional[List[Dict[str, Any]]] = None,
    sub_results: Optional[List[Dict[str, Any]]] = None,
    maturity: str = "core",
    null_reason: Optional[str] = None,
    null_owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a DimensionResult. score is always null / uncalibrated."""
    out: Dict[str, Any] = {
        "id": lens_id,
        "status": status,
        "score": None,
        "score_reason": "uncalibrated",
        "confidence": confidence,
        "hard_gate_failures": hard_gate_failures or [],
        "findings": findings or [],
        "raw_metrics": raw_metrics or {},
        "evidence_refs": evidence_refs or [],
        "analysis_scope": analysis_scope or empty_scope(),
        "tools": tools or [],
        "suppressions_applied": suppressions_applied or [],
        "maturity": maturity,
        "rubric_id": None,
    }
    if sub_results is not None:
        out["sub_results"] = sub_results
    if status == "null":
        if not null_reason or not null_owner:
            raise ValueError("null status requires null_reason and null_owner")
        out["null_reason"] = null_reason
        out["null_owner"] = null_owner
        out["null_display"] = "UNDECLARED" if null_owner == "subject" else "NOT MEASURED"
    return out


def undeclared(
    lens_id: str,
    *,
    maturity: str = "core",
    raw_metrics: Optional[Dict[str, Any]] = None,
    evidence_refs: Optional[List[str]] = None,
    analysis_scope: Optional[Dict[str, Any]] = None,
    null_reason: str = "not_declared_by_subject",
) -> Dict[str, Any]:
    return dimension(
        lens_id=lens_id,
        status="null",
        confidence="low",
        maturity=maturity,
        null_reason=null_reason,
        null_owner="subject",
        raw_metrics=raw_metrics,
        evidence_refs=evidence_refs,
        analysis_scope=analysis_scope,
    )


def not_measured(
    lens_id: str,
    *,
    null_reason: str,
    maturity: str = "core",
    raw_metrics: Optional[Dict[str, Any]] = None,
    evidence_refs: Optional[List[str]] = None,
    analysis_scope: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return dimension(
        lens_id=lens_id,
        status="null",
        confidence="low",
        maturity=maturity,
        null_reason=null_reason,
        null_owner="metria",
        raw_metrics=raw_metrics,
        evidence_refs=evidence_refs,
        analysis_scope=analysis_scope,
        tools=tools,
    )


def aggregate_overall(lenses: Dict[str, Dict[str, Any]]) -> str:
    """Hard-gate aggregation only. Null lenses do not pull pass or fail."""
    hard = []
    for lens in lenses.values():
        hard.extend(lens.get("hard_gate_failures") or [])
    if hard:
        return "fail"
    statuses = [l["status"] for l in lenses.values() if l["status"] != "null"]
    if any(s == "fail" for s in statuses):
        return "fail"
    if any(s == "warn" for s in statuses):
        return "warn"
    if statuses and all(s == "pass" for s in statuses):
        return "pass"
    # only nulls measured → treat as warn (incomplete cold run), not pass
    return "warn"


def top_risks_from_lenses(lenses: Dict[str, Dict[str, Any]], limit: int = 5) -> List[Dict[str, str]]:
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    items = []
    for lens in lenses.values():
        for finding in lens.get("findings") or []:
            items.append(
                (
                    severity_rank.get(finding.get("severity", "info"), 9),
                    finding.get("fingerprint", ""),
                    finding.get("title", ""),
                )
            )
        for gate in lens.get("hard_gate_failures") or []:
            items.append((0, f"hardgate:{lens['id']}", gate))
    items.sort(key=lambda x: (x[0], x[1]))
    out = []
    seen = set()
    for _, fp, title in items:
        if fp in seen:
            continue
        seen.add(fp)
        out.append({"summary": title, "finding_fingerprint": fp})
        if len(out) >= limit:
            break
    return out
