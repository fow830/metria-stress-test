"""Subject discovery — analysis only, no subject code execution."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Set

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".eggs",
}


def iter_files(root: Path) -> List[Path]:
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            out.append(Path(dirpath) / name)
    return out


def count_loc(root: Path) -> Dict[str, int]:
    py_loc = 0
    total_loc = 0
    py_files = 0
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        total_loc += lines
        if path.suffix == ".py":
            py_loc += lines
            py_files += 1
    return {"py_loc": py_loc, "total_loc": total_loc, "py_files": py_files}


def find_lockfiles(root: Path) -> List[str]:
    names = [
        "requirements.txt",
        "requirements-dev.txt",
        "dev-requirements.txt",
        "Pipfile.lock",
        "poetry.lock",
        "pdm.lock",
        "uv.lock",
        "constraints.txt",
    ]
    found = []
    for path in iter_files(root):
        if path.name in names or path.name.startswith("requirements") and path.suffix == ".txt":
            rel = str(path.relative_to(root)).replace("\\", "/")
            if rel not in found:
                found.append(rel)
    # also pyproject.toml as dep source
    if (root / "pyproject.toml").is_file():
        found.append("pyproject.toml")
    return sorted(found)


def find_test_layout(root: Path) -> Dict[str, Any]:
    has_tests_dir = (root / "tests").is_dir() or (root / "test").is_dir()
    test_files = [
        str(p.relative_to(root)).replace("\\", "/")
        for p in iter_files(root)
        if p.name.startswith("test_") and p.suffix == ".py" or p.name.endswith("_test.py")
    ]
    pytest_ini = (root / "pytest.ini").is_file() or (root / "pyproject.toml").is_file()
    return {
        "has_tests_dir": has_tests_dir,
        "test_file_count": len(test_files),
        "test_files_sample": test_files[:20],
        "pytest_config_present": pytest_ini,
    }


def find_normative_artifacts(root: Path) -> Dict[str, Any]:
    openapi = []
    pact = []
    schemas = []
    for path in iter_files(root):
        rel = str(path.relative_to(root)).replace("\\", "/")
        low = rel.lower()
        if any(x in low for x in ("openapi", "swagger")) and path.suffix in {".yaml", ".yml", ".json"}:
            openapi.append(rel)
        if "pact" in low:
            pact.append(rel)
        if path.suffix == ".json" and "schema" in low:
            schemas.append(rel)
    return {"openapi": openapi[:20], "pact": pact[:20], "json_schema": schemas[:20]}


def find_architecture_rules(root: Path) -> Dict[str, Any]:
    markers = {
        "importlinter": False,
        "import_linter_ini": False,
        "archunit": False,
        "sonar_architecture": False,
    }
    for path in iter_files(root):
        name = path.name.lower()
        if name in {".importlinter", "setup.cfg"} or name == "pyproject.toml":
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "[importlinter]" in text or "importlinter" in text:
                markers["importlinter"] = True
            if name == ".importlinter":
                markers["import_linter_ini"] = True
        if "archunit" in name:
            markers["archunit"] = True
        if "sonar" in name and "architecture" in name:
            markers["sonar_architecture"] = True
    return markers


def find_sbom_practice(root: Path) -> List[str]:
    hits = []
    for path in iter_files(root):
        rel = str(path.relative_to(root)).replace("\\", "/")
        low = rel.lower()
        if any(x in low for x in ("sbom", "cyclonedx", "spdx")) and path.suffix in {
            ".json",
            ".xml",
            ".cdx",
            ".spdx",
        }:
            hits.append(rel)
    return hits[:20]


_REQ_LINE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)\s*(?:(==|>=|<=|~=|!=|>|<)\s*([A-Za-z0-9_.+\-]+))?"
)


def parse_requirements_txt(path: Path) -> List[Dict[str, str]]:
    pkgs = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return pkgs
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-"):
            continue
        m = _REQ_LINE.match(s)
        if not m:
            continue
        pkgs.append({"name": m.group(1), "specifier": (m.group(2) or "") + (m.group(3) or "")})
    return pkgs


def parse_poetry_lock_names(path: Path) -> List[Dict[str, str]]:
    """Minimal poetry.lock name/version scrape without toml dependency."""
    pkgs = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return pkgs
    name = None
    version = None
    for line in text.splitlines():
        if line.startswith("name = "):
            name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("version = ") and name:
            version = line.split("=", 1)[1].strip().strip('"')
            pkgs.append({"name": name, "specifier": f"=={version}"})
            name = None
            version = None
    return pkgs


def inventory_packages(root: Path) -> List[Dict[str, str]]:
    found: Dict[str, Dict[str, str]] = {}
    for rel in find_lockfiles(root):
        path = root / rel
        if path.name.endswith(".lock") and "poetry" in path.name:
            for pkg in parse_poetry_lock_names(path):
                found[pkg["name"].lower()] = pkg
        elif path.suffix == ".txt" or path.name.startswith("requirements"):
            for pkg in parse_requirements_txt(path):
                found[pkg["name"].lower()] = pkg
    return sorted(found.values(), key=lambda p: p["name"].lower())


def discover(root: Path) -> Dict[str, Any]:
    loc = count_loc(root)
    return {
        "root": str(root),
        "loc": loc,
        "lockfiles": find_lockfiles(root),
        "tests": find_test_layout(root),
        "normative": find_normative_artifacts(root),
        "architecture": find_architecture_rules(root),
        "sbom_files": find_sbom_practice(root),
        "packages": inventory_packages(root),
        "languages": ["python"] if loc["py_files"] else [],
    }
