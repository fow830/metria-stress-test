"""Subject discovery — analysis only, no subject code execution."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

# Path *segments* only — never substring match (B2: "test/" must not hit latest/attest/contest).
TEST_DIR_SEGMENTS = frozenset({"tests", "test", "testdata", "test_data", "fixtures"})


def iter_files(root: Path) -> List[Path]:
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            out.append(Path(dirpath) / name)
    return out


def existing_skip_dirs(root: Path) -> List[str]:
    """SKIP_DIRS entries that actually exist under root (N-9)."""
    found = []
    for name in sorted(SKIP_DIRS):
        if (root / name).exists():
            found.append(name)
    return found


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
        if path.name in names or (path.name.startswith("requirements") and path.suffix == ".txt"):
            rel = str(path.relative_to(root)).replace("\\", "/")
            if rel not in found:
                found.append(rel)
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


def _load_toml(path: Path) -> Optional[Dict[str, Any]]:
    try:
        import tomllib  # type: ignore[attr-defined]
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            return None
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except OSError:
        return None


def _dep_name_spec(item: str) -> Optional[Dict[str, str]]:
    s = item.strip()
    if not s or s.startswith((".", "/", "[")):
        return None
    # strip extras: name[extra]>=1
    m = re.match(
        r"^([A-Za-z0-9][A-Za-z0-9._\-]*)\s*(?:\[[^\]]*\])?\s*(.*)$",
        s,
    )
    if not m:
        return None
    name, rest = m.group(1), (m.group(2) or "").strip()
    spec = ""
    sm = re.match(r"^(==|>=|<=|~=|!=|>|<)\s*([A-Za-z0-9_.+\-]+)", rest)
    if sm:
        spec = sm.group(1) + sm.group(2)
    return {"name": name, "specifier": spec}


def parse_pyproject_runtime(path: Path) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Return ([runtime packages], error_or_none). error set when toml unavailable."""
    data = _load_toml(path)
    if data is None:
        return [], "tomllib_unavailable"
    pkgs: List[Dict[str, str]] = []
    project = data.get("project") or {}
    for item in project.get("dependencies") or []:
        if isinstance(item, str):
            parsed = _dep_name_spec(item)
            if parsed:
                pkgs.append(parsed)
    poetry = (data.get("tool") or {}).get("poetry") or {}
    deps = poetry.get("dependencies") or {}
    if isinstance(deps, dict):
        for name, val in deps.items():
            if name.lower() == "python":
                continue
            if isinstance(val, str):
                spec = val if val.startswith(("=", ">", "<", "!")) else ""
                if val.startswith("^") or val.startswith("~"):
                    spec = ""
                elif val.startswith("=="):
                    spec = val
                elif re.match(r"^\d", val):
                    spec = "==" + val
                pkgs.append({"name": name, "specifier": spec})
            elif isinstance(val, dict) and "version" in val:
                ver = str(val["version"]).lstrip("^~=")
                pkgs.append({"name": name, "specifier": f"=={ver}" if ver and ver[0].isdigit() else ""})
    return pkgs, None


def parse_setup_cfg_requires(path: Path) -> List[Dict[str, str]]:
    """Parse install_requires / requires-dist lists from setup.cfg."""
    pkgs: List[Dict[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return pkgs
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("install_requires") or stripped.startswith("requires-dist"):
            in_block = True
            if "=" in line:
                rhs = line.split("=", 1)[1].strip().strip("[]")
                if rhs:
                    parsed = _dep_name_spec(rhs.strip("'\""))
                    if parsed:
                        pkgs.append(parsed)
            continue
        if in_block:
            if not line.startswith((" ", "\t")) or stripped.startswith("["):
                break
            if not stripped or stripped.startswith("#"):
                continue
            parsed = _dep_name_spec(stripped.strip("'\"").rstrip(","))
            if parsed:
                pkgs.append(parsed)
    return pkgs


def parse_setup_py_requires(path: Path) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Static AST scrape of setup(install_requires=[...]). Returns (pkgs, error)."""
    try:
        import ast
    except ImportError:
        return [], "ast_unavailable"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return [], f"read_error:{exc}"
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [], f"syntax_error:{exc}"

    pkgs: List[Dict[str, str]] = []

    def _from_list(node: ast.AST) -> None:
        if not isinstance(node, (ast.List, ast.Tuple)):
            return
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                # Drop environment markers after ';'
                raw = elt.value.split(";", 1)[0].strip()
                parsed = _dep_name_spec(raw)
                if parsed:
                    pkgs.append(parsed)

    for node in ast.walk(tree):
        # setup(..., install_requires=[...]) or setuptools.setup(...)
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in {"setup", "setuptools"}:
                for kw in node.keywords:
                    if kw.arg in {"install_requires", "requires"}:
                        _from_list(kw.value)
        # install_requires = [...] assignment
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "install_requires":
                    _from_list(node.value)
    return pkgs, None


def declared_optional_extras(root: Path) -> List[str]:
    """Names of optional-dependency / extras_require extras declared by the subject."""
    extras: List[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = _load_toml(pyproject)
        if data:
            opt = (data.get("project") or {}).get("optional-dependencies") or {}
            if isinstance(opt, dict):
                extras.extend(str(k) for k in opt.keys())
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        try:
            text = setup_cfg.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        in_extras = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower() in {"[options.extras_require]", "[extras_require]"}:
                in_extras = True
                continue
            if in_extras:
                if stripped.startswith("["):
                    break
                if "=" in stripped and not stripped.startswith((" ", "\t", "#")):
                    extras.append(stripped.split("=", 1)[0].strip())
    return sorted(set(extras))


def classify_req_path(rel: str) -> str:
    """Classify a requirements/lock path: runtime | dev | docs | example | test."""
    low = rel.replace("\\", "/").lower()
    parts = low.split("/")
    if "examples" in parts or "example" in parts:
        return "example"
    if "docs" in parts or "doc" in parts:
        return "docs"
    if any(p in {"tests", "test", "testdata", "fixtures"} for p in parts):
        return "test"
    base = parts[-1]
    if any(x in base for x in ("dev", "doc")):
        return "dev"
    if "test" in base:
        return "test"
    if base == "pyproject.toml":
        return "runtime"
    if base.endswith(".lock"):
        return "runtime"
    # Root requirements.txt is often tooling when pyproject declares runtime — caller decides.
    return "runtime"


def inventory_packages(root: Path) -> List[Dict[str, str]]:
    """Inventory with source classification. CVE lens should use class==runtime only.

    Extra key ``_meta`` is not returned; callers that need parse gaps should use
    ``inventory_packages_with_meta``.
    """
    pkgs, _meta = inventory_packages_with_meta(root)
    return pkgs


def inventory_packages_with_meta(root: Path) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """Like inventory_packages, plus meta about unreadable sources (setup.py gaps)."""
    found: Dict[str, Dict[str, str]] = {}
    meta: Dict[str, Any] = {"setup_py_present": False, "setup_py_error": None, "sources_read": []}
    pyproject = root / "pyproject.toml"
    has_declared_runtime = False
    if pyproject.is_file():
        runtime_pkgs, err = parse_pyproject_runtime(pyproject)
        if err is None:
            for pkg in runtime_pkgs:
                entry = {
                    "name": pkg["name"],
                    "specifier": pkg.get("specifier") or "",
                    "class": "runtime",
                    "source": "pyproject.toml",
                }
                found[pkg["name"].lower()] = entry
                has_declared_runtime = True
            meta["sources_read"].append("pyproject.toml")
        else:
            meta["pyproject_error"] = err

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        for pkg in parse_setup_cfg_requires(setup_cfg):
            key = pkg["name"].lower()
            if key not in found:
                found[key] = {
                    "name": pkg["name"],
                    "specifier": pkg.get("specifier") or "",
                    "class": "runtime",
                    "source": "setup.cfg",
                }
                has_declared_runtime = True
        meta["sources_read"].append("setup.cfg")

    setup_py = root / "setup.py"
    if setup_py.is_file():
        meta["setup_py_present"] = True
        setup_pkgs, setup_err = parse_setup_py_requires(setup_py)
        if setup_err:
            meta["setup_py_error"] = setup_err
        else:
            meta["sources_read"].append("setup.py")
            for pkg in setup_pkgs:
                key = pkg["name"].lower()
                if key not in found:
                    found[key] = {
                        "name": pkg["name"],
                        "specifier": pkg.get("specifier") or "",
                        "class": "runtime",
                        "source": "setup.py",
                    }
                    has_declared_runtime = True

    for rel in find_lockfiles(root):
        if rel == "pyproject.toml":
            continue
        path = root / rel
        cls = classify_req_path(rel)
        # When packaging metadata already gave runtime deps, treat root requirements*.txt as non-runtime.
        if has_declared_runtime and cls == "runtime" and path.suffix == ".txt":
            cls = "dev"
        if path.name.endswith(".lock") and "poetry" in path.name:
            for pkg in parse_poetry_lock_names(path):
                key = pkg["name"].lower()
                if key in found and found[key].get("class") == "runtime":
                    # Prefer pinned version from lock for runtime names we already track.
                    if pkg.get("specifier"):
                        found[key]["specifier"] = pkg["specifier"]
                    continue
                if key not in found:
                    found[key] = {
                        "name": pkg["name"],
                        "specifier": pkg.get("specifier") or "",
                        "class": cls,
                        "source": rel,
                    }
        elif path.suffix == ".txt" or path.name.startswith("requirements"):
            for pkg in parse_requirements_txt(path):
                key = pkg["name"].lower()
                if key in found and found[key].get("class") == "runtime" and cls != "runtime":
                    continue
                if key in found and found[key].get("class") == "runtime":
                    if pkg.get("specifier") and not found[key].get("specifier"):
                        found[key]["specifier"] = pkg["specifier"]
                    continue
                if key not in found or _class_rank(cls) < _class_rank(found[key].get("class") or "dev"):
                    found[key] = {
                        "name": pkg["name"],
                        "specifier": pkg.get("specifier") or "",
                        "class": cls,
                        "source": rel,
                    }
    return sorted(found.values(), key=lambda p: p["name"].lower()), meta


def _class_rank(cls: str) -> int:
    order = {"runtime": 0, "dev": 1, "test": 2, "docs": 3, "example": 4}
    return order.get(cls, 9)


def is_test_fixture_path(rel: str) -> bool:
    """True when a path segment is a test/fixture dir (or conftest.py), not a substring."""
    low = rel.replace("\\", "/").lower().strip("/")
    if not low:
        return False
    parts = [p for p in low.split("/") if p]
    if not parts:
        return False
    fname = parts[-1]
    if fname == "conftest.py" or fname.startswith("conftest."):
        return True
    for seg in parts:
        if seg in TEST_DIR_SEGMENTS:
            return True
        # test_foo/ as a directory segment (not files like test_x.py at repo root alone)
        if seg.startswith("test_") and seg != fname:
            return True
    return False


def discover(root: Path) -> Dict[str, Any]:
    loc = count_loc(root)
    return {
        # Portable — never absolute host tmp paths (B4).
        "root": ".",
        "loc": loc,
        "lockfiles": find_lockfiles(root),
        "tests": find_test_layout(root),
        "normative": find_normative_artifacts(root),
        "architecture": find_architecture_rules(root),
        "sbom_files": find_sbom_practice(root),
        "packages": inventory_packages(root),
        "languages": ["python"] if loc["py_files"] else [],
    }
