"""r3: `--no-tests` must mean zero subject code execution (r1 B1.2).

supply_chain.resolve_runtime_versions() runs `pip install -e .` on the subject, i.e.
executes its setup.py / build backend, regardless of --no-tests. pip may be offline on the
reviewer box, so we record the argv that metria sends to subprocesses instead of relying
on the build actually succeeding.
"""
import sys, json, pathlib, subprocess, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import metria.sandbox as sb
import metria.adapters.supply_chain as sc
from metria.runner import load_policy, run_subject

calls = []
real = sb.run_scrubbed
def spy(argv, **kw):
    calls.append(list(map(str, argv)))
    return real(argv, **kw)
sc.run_scrubbed = spy

S = pathlib.Path(tempfile.mkdtemp()) / "subj"; (S / "tests").mkdir(parents=True)
(S / "setup.py").write_text("import pathlib; pathlib.Path('/tmp/metria-r3-setup-py-ran').write_text('x')\n"
                            "from setuptools import setup; setup(name='s', version='0', install_requires=['requests>=2.0'])\n")
(S / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
for a in (["init", "-q"], ["add", "-A"], ["-c", "user.email=p@p", "-c", "user.name=p", "commit", "-qm", "i"]):
    subprocess.run(["git", *a], cwd=S, check=True)

out = S.parent / "out"
run_subject(S, out_dir=out, policy=load_policy(ROOT / "policy" / "cold_external.yaml"),
            repo_url="probe", commit_sha=None, query_osv=True, run_tests=False)
pip_installs = [c for c in calls if "install" in c and "-e" in c]
print("run_tests=False; subject editable installs issued by supply_chain:")
for c in pip_installs:
    print("   ", " ".join(pathlib.Path(x).name if "/" in x else x for x in c))
print("setup.py side effect present:", pathlib.Path("/tmp/metria-r3-setup-py-ran").exists(),
      "(False here may just mean pip was offline before build; the argv above is the defect)")
