"""Subject-process isolation — scrubbed env + optional venv. Never inherit operator secrets."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

# Allowlist only. Tokens/keys/secrets/AWS/GH/GIT credentials never pass through.
# PYTHONPATH/PYTHONHOME omitted — operator import-path injection must not reach subject.
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
    "PYTHONNOUSERSITE",
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
    out["PYTHONNOUSERSITE"] = "1"
    if extra:
        for k, v in extra.items():
            if k.upper() in {x.upper() for x in _ENV_ALLOW} or k.startswith("METRIA_"):
                out[k] = v
    return out


_METRIA_TMP_RE = re.compile(
    r"(?:"
    r"(?:/private)?/var/folders/[^\s:\"']+"
    r"|/var/tmp"
    r"|/tmp"
    r"|C:\\Users\\[^\\]+\\AppData\\Local\\Temp"
    r")"
    r"[/\\]metria-(?:adeq|home|subject|worktree|corpus)-[^\s:\"']*",
    re.I,
)
_GENERIC_MAC_FOLDER = re.compile(r"(?:/private)?/var/folders/[A-Za-z0-9_./\-]+")


def _path_variants(path: Path) -> List[str]:
    out: List[str] = [str(path)]
    try:
        resolved = path.resolve()
        out.append(str(resolved))
        s = str(resolved)
        if s.startswith("/private/"):
            out.append(s[len("/private") :])
        elif s.startswith("/var/"):
            out.append("/private" + s)
    except OSError:
        pass
    return [v for v in out if v]


def scrub_text_paths(
    text: str,
    root: Path,
    *,
    extra_roots: Optional[Sequence[Path]] = None,
) -> str:
    """Replace absolute subject/tmp paths so evidence is portable (B4 / N-r2-3)."""
    if not text:
        return text
    subject_vars = sorted(set(_path_variants(root)), key=len, reverse=True)
    for v in subject_vars:
        text = text.replace(v, "<subject>")
    other: List[str] = []
    for r in extra_roots or ():
        other.extend(_path_variants(r))
    try:
        other.extend(_path_variants(Path(tempfile.gettempdir())))
    except OSError:
        pass
    for v in sorted(set(other), key=len, reverse=True):
        # Only replace when still present as a concrete absolute prefix of a metria run dir;
        # blanket tempdir replace would destroy unrelated /tmp mentions — regex handles those.
        if "/metria-" in v or "\\metria-" in v:
            text = text.replace(v, "<metria-tmp>")
    text = _METRIA_TMP_RE.sub("<metria-tmp>", text)
    text = _GENERIC_MAC_FOLDER.sub("<tmp>", text)
    try:
        td = str(Path(tempfile.gettempdir()).resolve())
        text = re.sub(
            re.escape(td) + r"[/\\]metria-(?:adeq|home|subject|worktree|corpus)-[^\s:\"']*",
            "<metria-tmp>",
            text,
            flags=re.I,
        )
    except OSError:
        pass
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


def _declared_extras(root: Path) -> Set[str]:
    extras: Set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            from metria.discovery import parse_pyproject_extras

            extras.update(parse_pyproject_extras(pyproject))
        except Exception:  # noqa: BLE001
            pass
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        try:
            from metria.discovery import parse_setup_cfg_extras

            extras.update(parse_setup_cfg_extras(setup_cfg))
        except Exception:  # noqa: BLE001
            pass
    return {e.lower() for e in extras}


def discover_install_targets(root: Path) -> List[List[str]]:
    """Ordered pip install argument lists for subject test deps (best-effort).

    Only emits ``.[extra]`` for extras that metadata actually declares. After any
    editable install, requirements*{test,dev}*.txt targets still follow (N-r2-2).
    """
    targets: List[List[str]] = []
    pyproject = root / "pyproject.toml"
    setup_py = root / "setup.py"
    setup_cfg = root / "setup.cfg"
    has_packaging = pyproject.is_file() or setup_py.is_file() or setup_cfg.is_file()
    declared = _declared_extras(root) if has_packaging else set()
    if has_packaging:
        for extra in ("test", "tests", "dev", "testing"):
            if extra in declared:
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


_MISSING_EXTRA = re.compile(r"does not provide the extra", re.I)


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

    # Install editable (declared extras only) then ALWAYS continue to -r files.
    for args in discover_install_targets(root):
        proc = run_scrubbed(
            [str(pip), "install", "-q", *args],
            cwd=root,
            env=env,
            timeout=timeout_s,
        )
        stderr = proc.stderr or ""
        missing_extra = bool(_MISSING_EXTRA.search(stderr))
        entry = {
            "args": args,
            "returncode": proc.returncode,
            "stderr_tail": stderr[-500:],
            "missing_extra": missing_extra,
        }
        log["attempts"].append(entry)
        if proc.returncode == 0 and not missing_extra:
            log["installed"].append(" ".join(args))
            log["ok"] = True
            # Do not break after editable — requirements-dev/test must still run.
            continue
        # Missing-extra warning with rc=0 is a failed attempt for that target.
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
        import json

        data = json.loads(proc.stdout or "[]")
    except (json.JSONDecodeError, ValueError):
        return {}
    out: Dict[str, str] = {}
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            ver = str(row.get("version") or "").strip()
            if name and ver:
                out[name.lower().replace("_", "-")] = ver
    return out


def cleanup_dir(path: Optional[Path]) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)
