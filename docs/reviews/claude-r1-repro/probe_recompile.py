"""N: re.compile() is flagged as 'Use of compile() on dynamic input' and alone flips structural to WARN."""
import sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from metria.adapters import structural
d = pathlib.Path(tempfile.mkdtemp()); (d/"m.py").write_text("import re\nPAT = re.compile(r'^a+$')\n")
r = structural.run_structural(d, d/"ev")
print("status:", r["status"], [f["title"] for f in r["findings"]])
