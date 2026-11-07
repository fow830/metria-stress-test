"""B2: OSV network failure is reported as supply_chain PASS (fail-open)."""
import sys, pathlib, tempfile, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from metria.adapters import supply_chain
def boom(*a, **k): raise urllib.error.URLError("network down (probe)")
import urllib.error; urllib.request.urlopen = boom
d = pathlib.Path(tempfile.mkdtemp()); (d/"requirements.txt").write_text("jinja2==3.1.2\nurllib3==1.26.0\n")
r = supply_chain.run_supply_chain(d, d/"ev", query_osv=True)
print("status:", r["status"], "| osv_errors:", r["raw_metrics"]["osv_errors"], "| osv_queries:", r["raw_metrics"]["osv_queries"])
