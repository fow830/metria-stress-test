"""r2: _looks_like_env_failure() turns genuine subject failures into NOT MEASURED.

Case A: subject's own package has a broken internal import (a real product defect).
Case B: one real assertion failure + any test whose output mentions 'ImportError'.
Same venv stub as probe_env_leak_run_mode.py (pytest from system site-packages).
"""
import sys, pathlib, subprocess, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import metria.adapters.test_adequacy as ta
def venv_sys(work):
    d = work / "venv"; subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(d)], check=True)
    return d / "bin" / "python", d / "bin" / "pip"
ta.create_subject_venv = venv_sys
ta.install_subject_test_deps = lambda *a, **k: {"installed": ["(stubbed)"], "attempts": [], "ok": True}

def subject(files):
    S = pathlib.Path(tempfile.mkdtemp()) / "s"
    for rel, txt in files.items():
        p = S / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(txt)
    return S

A = subject({
  "pkg/__init__.py": "",
  "pkg/core.py": "from pkg.helperz import twice  # typo: real subject bug\n",
  "pkg/helpers.py": "def twice(x): return 2*x\n",
  "tests/test_core.py": "def test_core():\n    import pkg.core\n    assert pkg.core.twice(2) == 4\n",
})
B = subject({
  "tests/test_a.py": "def test_real_bug():\n    assert 1 + 1 == 3\n",
  "tests/test_b.py": "def test_msg():\n    raise RuntimeError('ImportError while loading plugin is expected to be handled')\n",
})
for name, S in (("A broken internal import", A), ("B real assertion + 'ImportError' text", B)):
    r = ta.run_test_adequacy(S, S.parent / "ev", run_tests=True)
    print(f"{name:40} -> status={r['status']} null_reason={r.get('null_reason')} display={r.get('null_display')}")
