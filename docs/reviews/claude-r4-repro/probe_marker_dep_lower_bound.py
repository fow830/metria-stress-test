"""r4: a runtime dep excluded by its environment marker (not installed on this runner) is
OSV-queried at its range *lower bound* (range_fallback) — a version nobody installs."""
import sys, pathlib, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import metria.adapters.supply_chain as sc
queried = []
sc._osv_query = lambda name, version, timeout=12.0: (queried.append(f"{name}=={version}") or ([], None))
sc.pip_list_versions = lambda pip, env, timeout_s=60: {"werkzeug": "3.1.9"}   # what the venv really has
class P:  # successful no-op subprocess
    returncode = 0; stdout = ""; stderr = ""
sc.run_scrubbed = lambda *a, **k: P()
d = pathlib.Path(tempfile.mkdtemp())
(d / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\n'
  'dependencies=["werkzeug>=3.0.0", "importlib-metadata>=3.6.0; python_version < \'3.10\'"]\n')
r = sc.run_supply_chain(d, d / "ev", query_osv=True, allow_subject_install=True)
import json; meta = json.loads((d / "ev" / "resolved_versions.json").read_text())["meta"]
print("OSV queried:", queried, "| range_fallback:", meta.get("range_fallback"))
