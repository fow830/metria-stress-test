"""Structural adapter — AST danger patterns + secret heuristics + optional ruff."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from metria.discovery import SKIP_DIRS, count_loc, iter_files
from metria.model import dimension

SECRET_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("generic_api_key_assign", re.compile(r"(?i)(api[_-]?key|secret|token)\s*=\s*['\"][A-Za-z0-9_\-]{16,}")),
    ("private_key_header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

DANGEROUS_CALLS = {
    "eval": ("high", "Use of eval()"),
    "exec": ("high", "Use of exec()"),
    "compile": ("medium", "Use of compile() on dynamic input"),
}


class _DangerVisitor(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.findings: List[Dict[str, Any]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in DANGEROUS_CALLS:
            sev, title = DANGEROUS_CALLS[name]
            self.findings.append(self._finding(f"ast:{name}", title, sev, node.lineno, ["ast"]))
        # subprocess(..., shell=True)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"run", "Popen", "call", "check_output"}:
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    self.findings.append(
                        self._finding(
                            "ast:shell_true",
                            "subprocess call with shell=True",
                            "high",
                            node.lineno,
                            ["ast"],
                        )
                    )
        if isinstance(node.func, ast.Attribute) and node.func.attr == "loads":
            # pickle.loads
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "pickle":
                self.findings.append(
                    self._finding("ast:pickle_loads", "pickle.loads()", "high", node.lineno, ["ast"])
                )
        self.generic_visit(node)

    def _finding(self, kind: str, title: str, severity: str, line: int, detectors: List[str]) -> Dict[str, Any]:
        fp = hashlib.sha256(f"{kind}:{self.rel}:{line}:{title}".encode()).hexdigest()[:16]
        return {
            "fingerprint": f"struct:{kind}:{fp}",
            "title": f"{title} at {self.rel}:{line}",
            "severity": severity,
            "detectors": detectors,
            "confirmation": "unconfirmed",
            "lens_routing_rule": "sast_or_secret_to_structural",
            "evidence_refs": [f"structural/{kind}.json"],
        }


def _scan_secrets(root: Path) -> List[Dict[str, Any]]:
    findings = []
    for path in iter_files(root):
        if path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".whl"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        for kind, pattern in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                fp = hashlib.sha256(f"{kind}:{rel}:{line}".encode()).hexdigest()[:16]
                findings.append(
                    {
                        "fingerprint": f"struct:secret:{kind}:{fp}",
                        "title": f"Possible secret ({kind}) at {rel}:{line}",
                        "severity": "critical",
                        "detectors": ["secret_regex"],
                        "confirmation": "unconfirmed",
                        "lens_routing_rule": "secret_head_to_structural",
                        "evidence_refs": ["structural/secrets.json"],
                    }
                )
                if len(findings) >= 50:
                    return findings
    return findings


def _scan_ast(root: Path) -> List[Dict[str, Any]]:
    findings = []
    for path in iter_files(root):
        if path.suffix != ".py":
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(src, filename=rel)
        except (OSError, SyntaxError):
            continue
        visitor = _DangerVisitor(rel)
        visitor.visit(tree)
        findings.extend(visitor.findings)
        if len(findings) >= 100:
            break
    return findings


def _run_ruff(root: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    tools = []
    findings: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {"ruff_ran": False, "ruff_issue_count": 0}
    try:
        ver = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if ver.returncode != 0:
            return findings, metrics, tools
        version = (ver.stdout or ver.stderr or "unknown").strip()
        tools.append({"name": "ruff", "version": version, "ruleset_digest": None})
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--output-format", "json", str(root)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        metrics["ruff_ran"] = True
        if not proc.stdout.strip():
            return findings, metrics, tools
        data = json.loads(proc.stdout)
        metrics["ruff_issue_count"] = len(data)
        # Keep only security-ish / bugbear codes as structural signal; do not dump style noise.
        interesting = {"S", "B", "E9", "F821", "F822", "F823"}
        for item in data[:40]:
            code = str(item.get("code") or "")
            if not any(code.startswith(p) for p in interesting):
                continue
            loc = item.get("location") or {}
            path = item.get("filename") or ""
            try:
                rel = str(Path(path).resolve().relative_to(root.resolve())).replace("\\", "/")
            except Exception:
                rel = path
            line = loc.get("row") or 0
            msg = item.get("message") or code
            fp = hashlib.sha256(f"ruff:{code}:{rel}:{line}".encode()).hexdigest()[:16]
            findings.append(
                {
                    "fingerprint": f"struct:ruff:{code}:{fp}",
                    "title": f"ruff {code}: {msg} at {rel}:{line}",
                    "severity": "medium" if code.startswith("S") else "low",
                    "detectors": ["ruff"],
                    "confirmation": "unconfirmed",
                    "lens_routing_rule": "sast_or_secret_to_structural",
                    "evidence_refs": ["structural/ruff.json"],
                }
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        metrics["ruff_error"] = str(exc)
    return findings, metrics, tools


def run_structural(root: Path, evidence_dir: Path) -> Dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    loc = count_loc(root)
    secret_findings = _scan_secrets(root)
    ast_findings = _scan_ast(root)
    ruff_findings, ruff_metrics, ruff_tools = _run_ruff(root)

    (evidence_dir / "secrets.json").write_text(
        json.dumps(secret_findings, indent=2), encoding="utf-8"
    )
    (evidence_dir / "ast.json").write_text(json.dumps(ast_findings, indent=2), encoding="utf-8")
    (evidence_dir / "ruff.json").write_text(json.dumps(ruff_findings, indent=2), encoding="utf-8")

    findings = secret_findings + ast_findings + ruff_findings
    hard = []
    # HEAD-tree secrets are hard fail only when pattern is high-confidence key material
    for f in secret_findings:
        if "aws_access_key_id" in f["fingerprint"] or "private_key_header" in f["fingerprint"]:
            hard.append(f["title"])

    high = sum(1 for f in findings if f.get("severity") in {"high", "critical"})
    if hard:
        status = "fail"
    elif high:
        status = "warn"
    elif findings:
        status = "warn"
    else:
        status = "pass"

    tools = [{"name": "metria-ast", "version": "0.1.0", "ruleset_digest": None}] + ruff_tools
    tools.append({"name": "metria-secret-regex", "version": "0.1.0", "ruleset_digest": None})

    return dimension(
        lens_id="structural",
        status=status,
        confidence="medium",
        findings=findings,
        hard_gate_failures=hard,
        raw_metrics={
            "finding_count": len(findings),
            "high_or_critical": high,
            "secrets_head": len(secret_findings),
            "ast_findings": len(ast_findings),
            **ruff_metrics,
            "py_files": loc["py_files"],
        },
        evidence_refs=[
            "structural/secrets.json",
            "structural/ast.json",
            "structural/ruff.json",
        ],
        analysis_scope={
            "languages_analyzed": ["python"] if loc["py_files"] else [],
            "loc_analyzed": loc["py_loc"],
            "loc_total": loc["total_loc"],
            "excluded_paths": sorted(SKIP_DIRS),
            "exclusion_reasons": {d: "tooling_or_vcs" for d in sorted(SKIP_DIRS)},
        },
        tools=tools,
        maturity="core",
    )
