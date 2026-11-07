"""Subject-process isolation — scrubbed env + optional venv. Never inherit operator secrets."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from metria.discovery import declared_optional_extras

# Allowlist only. Tokens/keys/secrets/AWS/GH/GIT credentials never pass through.
# PYTHONPATH / PYTHONHOME intentionally omitted (N-r2-1) — operator import hijack channel.
_ENV_ALLOW = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TZ",
    "TERM",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "PYTHONWARNINGS",
    "VIRTUAL_ENV",
}


def scrub_env(
    *,
    home: Optional[Path] = None,
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build a minimal env for subject subprocesses (no operator secrets)."""
    out: Dict[str, str] = {}
    for key in _ENV_ALLOW:
        if key in os.environ and key not in {"HOME", "TMPDIR", "TEMP", "TMP", "VIRTUAL_ENV"}:
            out[key] = os.environ[key]
    if "PATH" not in out:
        out["PATH"] = "/usr/bin:/bin:/usr/local/bin"
    if "LANG" not in out:
        out["LANG"] = "C.UTF-8"
    home_path = home or Path(tempfile.mkdtemp(prefix="metria-home-"))
    out["HOME"] = str(home_path)
    tmp = home_path / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    out["TMPDIR"] = str(tmp)
    out["TEMP"] = str(tmp)
    out["TMP"] = str(tmp)
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    # N-r3-1: never expose operator user-site; clean venvs isolate imports.
    out["PYTHONNOUSERSITE"] = "1"
    if extra:
        allow = {x.upper() for x in _ENV_ALLOW} | {"PYTHONNOUSERSITE"}
        for k, v in extra.items():
            if k.upper() in allow or k.startswith("METRIA_"):
                out[k] = v
    return out


# Metria temp dirs anywhere under common temp roots — never bare "/tmp" replace (r3 B1).
_METRIA_TMP = re.compile(
    r"(?:/private)?(?:/var/folders/[^\s:\"']+|/tmp|/var/tmp|/private/tmp)"
    r"/metria-(?:adeq|home|subject|worktree|corpus|sca)-[A-Za-z0-9_]+[^\s:\"']*"
)
_MKTEMP_DIR = re.compile(
    r"(?:/private)?(?:/var/folders/[^\s:\"']+|/tmp|/var/tmp)/tmp\.[A-Za-z0-9_]+[^\s:\"']*"
)
_MAC_FOLDER_STUB = re.compile(r"(?:/private)?/var/folders/[A-Za-z0-9_./\-]+")


def _path_boundary_replace(text: str, path: str, repl: str) -> str:
    """Replace ``path`` only as a full path prefix (component boundary), not a substring."""
    if not path or len(path) < 4:
        return text
    # Escape and require end-of-path or a following path separator / word boundary.
    pat = re.compile(re.escape(path.rstrip("/")) + r"(?=/|\b|$)")
    return pat.sub(repl.rstrip("/"), text)


def scrub_text_paths(text: str, root: Path, *extra_roots: Path) -> str:
    """Replace absolute subject/tmp paths so evidence is portable (B4 / N-r2-3 / r3 B1)."""
    if not text:
        return text

    def _variants(p: Path) -> List[str]:
        out: List[str] = []
        try:
            resolved = p.resolve()
            out.append(str(resolved))
            s = str(resolved)
            if s.startswith("/private/"):
                out.append(s[len("/private") :])
            elif s.startswith("/var/") or s.startswith("/tmp/"):
                out.append("/private" + s)
        except OSError:
            pass
        out.append(str(p))
        return out

    # Exact known roots only — never bare gettempdir() like "/tmp" (r3 B1).
    subject_vars = set(_variants(root))
    other_vars: List[str] = []
    for r in extra_roots:
        for v in _variants(r):
            if v not in subject_vars:
                other_vars.append(v)
    for v in sorted(subject_vars, key=len, reverse=True):
        if v and len(v) > 3:
            text = _path_boundary_replace(text, v, "<subject>")
    for v in sorted(set(other_vars), key=len, reverse=True):
        if v and len(v) > 3:
            # Skip generic short temp roots (/tmp, /var/tmp).
            if v.rstrip("/") in {"/tmp", "/var/tmp", "/private/tmp"}:
                continue
            text = _path_boundary_replace(text, v, "<metria-tmp>")
    text = _METRIA_TMP.sub("<metria-tmp>", text)
    text = _MKTEMP_DIR.sub("<metria-tmp>", text)
    text = _MAC_FOLDER_STUB.sub("<tmp>", text)
    return text


def run_scrubbed(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=dict(env),
    )


def create_subject_venv(work: Path) -> Tuple[Path, Path]:
    """Create an empty venv under work; return (python, pip)."""
    venv_dir = work / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    if os.name == "nt":
        py = venv_dir / "Scripts" / "python.exe"
        pip = venv_dir / "Scripts" / "pip.exe"
    else:
        py = venv_dir / "bin" / "python"
        pip = venv_dir / "bin" / "pip"
    return py, pip


def discover_install_targets(root: Path) -> List[List[str]]:
    """Ordered pip install argument lists for subject test deps (best-effort).

    Only request extras that the subject actually declares. Always continue to
    requirements*{test,dev}*.txt after editable installs (N-r2-2).
    """
    targets: List[List[str]] = []
    pyproject = root / "pyproject.toml"
    setup_py = root / "setup.py"
    setup_cfg = root / "setup.cfg"
    has_packaging = pyproject.is_file() or setup_py.is_file() or setup_cfg.is_file()
    if has_packaging:
        extras = {e.lower() for e in declared_optional_extras(root)}
        for extra in ("test", "tests", "dev", "testing"):
            if extra in extras:
                targets.append(["-e", f".[{extra}]"])
        targets.append(["-e", "."])
    for name in (
        "requirements-test.txt",
        "requirements_test.txt",
        "test-requirements.txt",
        "requirements-dev.txt",
        "requirements_dev.txt",
        "dev-requirements.txt",
        "requirements.txt",
    ):
        path = root / name
        if path.is_file():
            targets.append(["-r", name])
    return targets


def _pip_missing_extra(stderr: str) -> bool:
    low = (stderr or "").lower()
    return "does not provide the extra" in low or "unknown extras" in low


def install_subject_test_deps(
    root: Path,
    pip: Path,
    env: Mapping[str, str],
    *,
    timeout_s: int = 300,
) -> Dict[str, object]:
    """Install pytest + best-effort subject test deps into the isolated venv."""
    log: Dict[str, object] = {"installed": [], "attempts": [], "ok": False}
    base = run_scrubbed(
        [str(pip), "install", "-q", "--upgrade", "pip", "pytest"],
        cwd=root,
        env=env,
        timeout=min(timeout_s, 120),
    )
    log["attempts"].append(
        {
            "args": ["pip", "install", "pip", "pytest"],
            "returncode": base.returncode,
            "stderr_tail": (base.stderr or "")[-500:],
        }
    )
    if base.returncode != 0:
        return log
    log["installed"].append("pytest")

    # N-r2-2: try every target; missing-extra warning (exit 0) is not success;
    # do not stop before requirements-dev.txt after a lucky editable.
    for args in discover_install_targets(root):
        proc = run_scrubbed(
            [str(pip), "install", "-q", *args],
            cwd=root,
            env=env,
            timeout=timeout_s,
        )
        missing = _pip_missing_extra(proc.stderr or "")
        entry = {
            "args": args,
            "returncode": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-500:],
            "missing_extra": missing,
        }
        log["attempts"].append(entry)
        if proc.returncode == 0 and not missing:
            log["installed"].append(" ".join(args))
            log["ok"] = True
    if not log["ok"] and log["installed"]:
        # pytest alone may still collect pure-stdlib suites
        log["ok"] = True
    return log


def pip_list_versions(pip: Path, env: Mapping[str, str], *, timeout_s: int = 60) -> Dict[str, str]:
    """Return {normalized_name: version} from ``pip list --format json``."""
    proc = run_scrubbed(
        [str(pip), "list", "--format", "json"],
        cwd=Path("."),
        env=env,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        return {}
    try:
        rows = json_loads_list(proc.stdout or "[]")
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        ver = str(row.get("version") or "").strip()
        if name and ver:
            out[name.lower().replace("_", "-")] = ver
    return out


def json_loads_list(text: str) -> List[dict]:
    import json

    data = json.loads(text)
    return data if isinstance(data, list) else []


def cleanup_dir(path: Optional[Path]) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)
