"""r4: subject-module detection misses src-layout packages whose import name differs
from the distribution name (Pillow/PIL, PyYAML/yaml, beautifulsoup4/bs4 pattern).
A broken *internal* import is then attributed to the runner env -> NOT MEASURED
(the r2 B1 failure mode, for a common layout).

Offline-safe stub (N-r4-1): system-site-packages + optional host .pth, no pip.
"""
import site
import sys
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import metria.adapters.test_adequacy as ta


def venv_sys(work):
    d = work / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(d)],
        check=True,
        capture_output=True,
    )
    py, pip = d / "bin" / "python", d / "bin" / "pip"
    check = subprocess.run(
        [str(py), "-c", "import pytest"],
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "HOME": str(work / "home")},
    )
    if check.returncode != 0:
        host = site.getusersitepackages()
        if isinstance(host, str) and pathlib.Path(host).is_dir():
            ver = f"{sys.version_info.major}.{sys.version_info.minor}"
            sp = d / "lib" / f"python{ver}" / "site-packages"
            if sp.is_dir():
                (sp / "_metria_host_usersite.pth").write_text(host + "\n", encoding="utf-8")
    return py, pip


ta.create_subject_venv = venv_sys
ta.install_subject_test_deps = lambda *a, **k: {"installed": ["(stubbed)"], "attempts": [], "ok": True}


def subject(files):
    S = pathlib.Path(tempfile.mkdtemp()) / "s"
    for rel, txt in files.items():
        p = S / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt)
    return S


common = {
    "src/imgkit/__init__.py": "",
    "src/imgkit/core.py": "from imgkit.helperz import twice  # typo: real subject bug\n",
    "src/imgkit/helpers.py": "def twice(x): return 2*x\n",
    "tests/conftest.py": (
        "import sys, pathlib; "
        "sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))\n"
    ),
    "tests/test_core.py": (
        "def test_core():\n"
        "    import imgkit.core\n"
        "    assert imgkit.core.twice(2) == 4\n"
    ),
}
cases = {
    "src layout, dist name == import name": {
        **common,
        "pyproject.toml": '[project]\nname = "imgkit"\nversion = "0"\n',
    },
    "src layout, dist name != import name": {
        **common,
        "pyproject.toml": '[project]\nname = "Imaging-Kit"\nversion = "0"\n',
    },
}
for name, files in cases.items():
    S = subject(files)
    r = ta.run_test_adequacy(S, S.parent / "ev", run_tests=True)
    print(
        f"{name:40} -> status={r['status']} null_reason={r.get('null_reason')} "
        f"subject_names={sorted(ta._subject_module_names(S))}"
    )
