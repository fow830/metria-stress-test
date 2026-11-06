"""r2: is_test_fixture_path() uses substring match, so real keys outside tests escape the hard gate."""
import sys, pathlib, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from metria.discovery import is_test_fixture_path
from metria.adapters import structural
for p in ["deploy/latest/server.key", "infra/attest/signing.pem", "src/contest/key.pem", "tests/certs/server.key"]:
    print(f"{p:32} fixture={is_test_fixture_path(p)}")
d = pathlib.Path(tempfile.mkdtemp()); k = d / "deploy" / "latest"; k.mkdir(parents=True)
(k / "server.key").write_text("-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n")
r = structural.run_structural(d, d / "ev")
print("real deploy key -> status:", r["status"], "| hard_gate_failures:", r["hard_gate_failures"])
