"""N: URL parsing treats userinfo '@' as a SHA separator."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from metria.cli import _parse_target
for t in ["https://github.com/pallets/click@006ae84d651846c944331fff78e340701fe58562",
          "https://oauth2@gitlab.example.com/group/repo"]:
    print(t, "->", _parse_target(t))
