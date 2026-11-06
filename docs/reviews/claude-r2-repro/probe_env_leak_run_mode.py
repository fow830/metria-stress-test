"""r2: env + filesystem reach of subject code in RUN mode (tests executed).

pip may be offline on the reviewer box, so the venv is created with
--system-site-packages (pytest available) and install is stubbed. Everything
else is the real metria code path (scrub_env, run_scrubbed, pytest in venv).
"""
import os, sys, json, pathlib, subprocess, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import metria.sandbox as sb
import metria.adapters.test_adequacy as ta

def venv_sys(work):
    d = work / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(d)], check=True)
    return d / "bin" / "python", d / "bin" / "pip"
ta.create_subject_venv = venv_sys
ta.install_subject_test_deps = lambda *a, **k: {"installed": ["(stubbed)"], "attempts": [], "ok": True}

T = pathlib.Path(tempfile.mkdtemp()); S = T / "subj"; (S / "tests").mkdir(parents=True)
sink = T / "seen.json"
real_home = pathlib.Path(os.path.expanduser("~"))
canary = real_home / ".metria-r2-canary"; canary.write_text("operator-file")
(S / "tests" / "conftest.py").write_text(f"""
import os, json, pathlib
json.dump({{
  "secret_like_env": sorted(k for k in os.environ if any(x in k for x in ("TOKEN","KEY","SECRET","AWS","GH_","GIT_"))),
  "PYTHONPATH": os.environ.get("PYTHONPATH"),
  "HOME": os.environ.get("HOME"),
  "can_read_operator_home_by_abs_path": pathlib.Path({str(canary)!r}).exists(),
}}, open({str(sink)!r}, "w"))
""")
(S / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
os.environ["FAKE_GITHUB_TOKEN"] = "ghp_probe"; os.environ["PYTHONPATH"] = "/operator/injected/path"
r = ta.run_test_adequacy(S, T / "ev", run_tests=True)
print("lens status:", r["status"], r.get("null_reason"))
print(json.dumps(json.load(open(sink)), indent=1) if sink.exists() else "conftest did not run")
canary.unlink()
