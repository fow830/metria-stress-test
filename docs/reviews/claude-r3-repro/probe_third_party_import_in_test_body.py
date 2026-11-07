"""r3: the six/_dbm case from r1 B3. A test fails ONLY because a non-subject module is
missing on the runner (ModuleNotFoundError raised inside the test body). That is runner env,
not a subject defect, yet it is classified `fail`.
Same venv stub as r2 probes (system site-packages; install stubbed)."""
import sys, pathlib, subprocess, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import metria.adapters.test_adequacy as ta
def venv_sys(work):
    d = work / "venv"; subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(d)], check=True)
    return d / "bin" / "python", d / "bin" / "pip"
ta.create_subject_venv = venv_sys
ta.install_subject_test_deps = lambda *a, **k: {"installed": ["(stubbed)"], "attempts": [], "ok": True}
S = pathlib.Path(tempfile.mkdtemp()) / "s"; (S / "tests").mkdir(parents=True)
(S / "mylib.py").write_text("def ok(): return 1\n")
(S / "tests" / "test_a.py").write_text("import mylib\ndef test_ok():\n    assert mylib.ok() == 1\n")
(S / "tests" / "test_b.py").write_text("def test_dbm_backend():\n    import _dbm_not_on_this_runner\n")
r = ta.run_test_adequacy(S, S.parent / "ev", run_tests=True)
print("only failure = missing non-subject module ->", "status:", r["status"], "| null_reason:", r.get("null_reason"),
      "| classify:", r["raw_metrics"].get("classify"))
