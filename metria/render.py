"""Human report renderer — UNDECLARED / NOT MEASURED, never bare NULL."""

from __future__ import annotations

from typing import Any, Dict


def _status_label(lens: Dict[str, Any]) -> str:
    status = lens.get("status")
    if status == "null":
        return lens.get("null_display") or (
            "UNDECLARED" if lens.get("null_owner") == "subject" else "NOT MEASURED"
        )
    return str(status).upper()


def render_report(run: Dict[str, Any]) -> str:
    subject = run["subject"]
    sha = subject["commit_sha"][:7]
    lines = [
        f"Metria — {run['overall'].upper()}                      commit {sha} · policy digest {run['policy_digest'][:18]}… · schema {run['schema_version']}",
        "",
    ]
    hard = []
    for lens in run["lenses"].values():
        for g in lens.get("hard_gate_failures") or []:
            hard.append(f"  {lens['id']:16} {g}")
    lines.append("HARD GATES")
    if hard:
        lines.extend(hard)
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("LENSES")
    for lens_id, lens in run["lenses"].items():
        label = _status_label(lens)
        extra = ""
        if lens_id == "normative":
            extra = "  [experimental]"
        metrics = lens.get("raw_metrics") or {}
        brief = ""
        if metrics:
            # compact one-liner of a few keys
            parts = []
            for key in (
                "finding_count",
                "high_or_critical",
                "secrets_head",
                "components",
                "suite",
                "strong_oracle",
                "test_file_count",
            ):
                if key in metrics:
                    parts.append(f"{key}={metrics[key]}")
            if parts:
                brief = "   " + " · ".join(parts[:4])
        lines.append(f"  {lens_id:16} {label:12}{brief}{extra}")
    lines.append("")
    lines.append("TOP RISKS")
    risks = run.get("top_risks") or []
    if not risks:
        lines.append("  (none)")
    else:
        for i, risk in enumerate(risks, 1):
            lines.append(f"  {i}. {risk['summary']}")
    pack = run.get("evidence_pack") or {}
    lines.append("")
    lines.append(
        f"evidence · sha256 {(pack.get('sha256') or '')[:12]}… · reproducible: metria run <repo>@{subject['commit_sha']} --profile cold_external"
    )
    lines.append("")
    return "\n".join(lines)
