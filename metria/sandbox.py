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

# Allowlist only. Tokens/keys/secrets/AWS/GH/GIT credentials never pass through.
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
    "PYTHONPATH",
    "PYTHONHOME",
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


_MAC_TMP = re.compile(
    r"(?:/private)?/var/folders/[^\s:\"']+/metria-(?:adeq|home|subject|worktree|corpus)-[^\s:\"']*"
)
_GENERIC_TMP = re.compile(
    r"(?:/private)?/var/folders/[A-Za-z0-9_./\-]+"
)


def scrub_text_paths(text: str, root: Path) -> str:
    """Replace absolute subject/tmp paths so evidence is portable (B4)."""
    if not text:
        return text
    variants = []
    try:
        resolved = root.resolve()
        variants.append(str(resolved))
        s = str(resolved)
        if s.startswith("/private/"):
            variants.append(s[len("/private") :])
        elif s.startswith("/var/"):
            variants.append("/private" + s)
    except OSError:
        variants.append(str(root))
    variants.append(str(root))
    for v in sorted(set(variants), key=len, reverse=True):
        if v:
            text = text.replace(v, "<subject>")
    # Scrub metria temp venvs / homes, then any leftover macOS folder stubs.
    text = _MAC_TMP.sub("<metria-tmp>", text)
    text = _GENERIC_TMP.sub("<tmp>", text)
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
    """Ordered pip install argument lists for subject test deps (best-effort)."""
    targets: List[List[str]] = []
    pyproject = root / "pyproject.toml"
    setup_py = root / "setup.py"
    setup_cfg = root / "setup.cfg"
    if pyproject.is_file() or setup_py.is_file() or setup_cfg.is_file():
        # Prefer declared test/dev extras when present; always try bare editable.
        for extra in ("test", "tests", "dev", "testing"):
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

    for args in discover_install_targets(root):
        proc = run_scrubbed(
            [str(pip), "install", "-q", *args],
            cwd=root,
            env=env,
            timeout=timeout_s,
        )
        entry = {
            "args": args,
            "returncode": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-500:],
        }
        log["attempts"].append(entry)
        if proc.returncode == 0:
            log["installed"].append(" ".join(args))
            log["ok"] = True
            # One successful subject install is enough; keep going only for -r test files after editable.
            if args[:1] == ["-e"]:
                break
    if not log["ok"] and log["installed"]:
        # pytest alone may still collect pure-stdlib suites
        log["ok"] = True
    return log


def cleanup_dir(path: Optional[Path]) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)
