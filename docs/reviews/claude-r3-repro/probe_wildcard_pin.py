"""r3: `httpcore==1.*` is parsed as pin `==1.` and OSV is queried for version "1."."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from metria.discovery import _dep_name_spec
from metria.adapters.supply_chain import _pinned_version
for s in ["httpcore==1.*", "foo~=2.1", "bar>=1,<2"]:
    d = _dep_name_spec(s); print(f"{s:16} -> spec={d['specifier']!r:10} pinned={_pinned_version(d['specifier'])!r}")
