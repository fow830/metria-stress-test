"""Structural adapter — AST danger patterns + secret heuristics + optional ruff."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from metria.discovery import count_loc, existing_skip_dirs, is_test_fixture_path, iter_files
from metria.model import dimension
from metria.sandbox import scrub_text_paths

SECRET_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("generic_api_key_assign", re.compile(r"(?i)(api[_-]?key|secret|token)\s*=\s*['\"][A-Za-z0-9_\-]{16,}")),
    ("private_key_header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

# Builtins only — Attribute calls like re.compile must not match (N-1).
DANGEROUS_BUILTINS = {
    "eval": ("high", "Use of eval()"),
    "exec": ("high", "Use of exec()"),
    "compile": ("medium", "Use of compile() on dynamic input"),
}


class _DangerVisitor(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.findings: List[Dict[str, Any]] = []

    def visit_Call(self, node: ast.Call) -> None:
        # N-1: only bare Name builtins (eval/exec/compile), never Attribute.attr
        if isinstance(node.func, ast.Name) and node.func.id in DANGEROUS_BUILTINS:
            sev, title = DANGEROUS_BUILTINS[node.func.id]
            self.findings.append(self._finding(f"ast:{node.func.id}", title, sev, node.lineno, ["ast"]))
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
        fixture = is_test_fixture_path(rel)
        for kind, pattern in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                fp = hashlib.sha256(f"{kind}:{rel}:{line}".encode()).hexdigest()[:16]
                finding: Dict[str, Any] = {
                    "fingerprint": f"struct:secret:{kind}:{fp}",
                    "title": f"Possible secret ({kind}) at {rel}:{line}",
                    "severity": "medium" if fixture else "critical",
                    "detectors": ["secret_regex"],
                    "confirmation": "unconfirmed",
                    "lens_routing_rule": "secret_head_to_structural",
                    "evidence_refs": ["structural/secrets.json"],
                }
                if fixture:
                    finding["title"] = f"Test-fixture key material ({kind}) at {rel}:{line}"
                findings.append(finding)
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


def _ruff_argv() -> Optional[List[str]]:
    """Prefer `python -m ruff`, else PATH ruff binary (B4)."""
    mod = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if mod.returncode == 0:
        return [sys.executable, "-m", "ruff"]
    path = shutil.which("ruff")
    if path:
        return [path]
    return None


def _run_ruff(root: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    tools: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    suppressions: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {"ruff_ran": False, "ruff_issue_count": 0, "ruff_absent": False}
    argv_base = _ruff_argv()
    if argv_base is None:
        metrics["ruff_absent"] = True
        tools.append({"name": "ruff", "version": "absent", "ruleset_digest": None})
        return findings, metrics, tools, suppressions
    try:
        ver = subprocess.run(
            [*argv_base, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        version = (ver.stdout or ver.stderr or "unknown").strip()
        tools.append({"name": "ruff", "version": version, "ruleset_digest": "metria-isolated-SBE9F82x"})
        # N-2: --isolated so subject noqa/config cannot silence; explicit select.
        proc = subprocess.run(
            [
                *argv_base,
                "check",
                "--isolated",
                "--select",
                "S,B,E9,F821,F822,F823",
                "--output-format",
                "json",
                str(root),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        metrics["ruff_ran"] = True
        raw_out = scrub_text_paths(proc.stdout or "", root)
        if not raw_out.strip():
            return findings, metrics, tools, suppressions
        data = json.loads(raw_out)
        metrics["ruff_issue_count"] = len(data)
        # Filter interesting first, THEN cap (N-2). Drop S101 on test paths (N-r3-5).
        interesting = []
        for item in data:
            code = str(item.get("code") or "")
            if code.startswith(("S", "B", "E9")) or code in {"F821", "F822", "F823"}:
                interesting.append(item)
        for item in interesting[:80]:
            code = str(item.get("code") or "")
            loc = item.get("location") or {}
            path = item.get("filename") or ""
            try:
                rel = str(Path(path).resolve().relative_to(root.resolve())).replace("\\", "/")
            except Exception:
                rel = scrub_text_paths(path, root)
            if code == "S101" and is_test_fixture_path(rel):
                continue
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
            if len(findings) >= 40:
                break
        # Note subject-config gaming surface: we force --isolated; record that.
        suppressions.append(
            {
                "rule": "ruff-isolated",
                "path": ".",
                "justification": "Metria runs ruff --isolated with fixed select; subject noqa/config ignored",
                "expires_at": None,
                "source": "policy",
            }
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        metrics["ruff_error"] = str(exc)
        metrics["ruff_absent"] = True
        if not tools:
            tools.append({"name": "ruff", "version": "absent", "ruleset_digest": None})
    return findings, metrics, tools, suppressions


def run_structural(root: Path, evidence_dir: Path) -> Dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    loc = count_loc(root)
    secret_findings = _scan_secrets(root)
    ast_findings = _scan_ast(root)
    ruff_findings, ruff_metrics, ruff_tools, ruff_suppressions = _run_ruff(root)

    (evidence_dir / "secrets.json").write_text(
        json.dumps(secret_findings, indent=2), encoding="utf-8"
    )
    (evidence_dir / "ast.json").write_text(json.dumps(ast_findings, indent=2), encoding="utf-8")
    (evidence_dir / "ruff.json").write_text(json.dumps(ruff_findings, indent=2), encoding="utf-8")

    findings = list(secret_findings + ast_findings + ruff_findings)
    hard: List[str] = []
    # B6: HEAD-tree secrets hard-fail only outside test fixtures.
    for f in secret_findings:
        title = f["title"]
        rel_part = ""
        if " at " in title:
            rel_part = title.split(" at ", 1)[-1].rsplit(":", 1)[0]
        if is_test_fixture_path(rel_part):
            continue
        if "aws_access_key_id" in f["fingerprint"] or "private_key_header" in f["fingerprint"]:
            hard.append(f["title"])

    if ruff_metrics.get("ruff_absent"):
        findings.append(
            {
                "fingerprint": "struct:ruff_absent",
                "title": "ruff detector absent on runner — structural coverage reduced",
                "severity": "low",
                "detectors": ["ruff"],
                "confirmation": "unconfirmed",
                "lens_routing_rule": "sast_or_secret_to_structural",
                "evidence_refs": ["structural/ruff.json"],
            }
        )

    high = sum(1 for f in findings if f.get("severity") in {"high", "critical"})
    if hard:
        status = "fail"
    elif high:
        status = "warn"
    elif findings:
        status = "warn"
    else:
        status = "pass"

    confidence = "low" if ruff_metrics.get("ruff_absent") else "medium"
    tools = [{"name": "metria-ast", "version": "0.1.0", "ruleset_digest": None}] + ruff_tools
    tools.append({"name": "metria-secret-regex", "version": "0.1.0", "ruleset_digest": None})
    excluded = existing_skip_dirs(root)

    return dimension(
        lens_id="structural",
        status=status,
        confidence=confidence,
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
            "excluded_paths": excluded,
            "exclusion_reasons": {d: "tooling_or_vcs" for d in excluded},
        },
        tools=tools,
        suppressions_applied=ruff_suppressions,
        maturity="core",
    )
