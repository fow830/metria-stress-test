#!/usr/bin/env bash
# Same subject, same SHA, two runs: compare finding fingerprints and evidence.zip sha256.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
T=$(mktemp -d); S="$T/subj"; mkdir -p "$S"
printf 'import re\nX=re.compile("a")\ndef add(a,b): return a+b\n' > "$S/m.py"
printf 'from m import add\ndef test_add(): assert add(1,2)==3\n' > "$S/test_m.py"
git -C "$S" init -q && git -C "$S" -c user.email=p@p -c user.name=p add -A && git -C "$S" -c user.email=p@p -c user.name=p commit -qm i
cd "$REPO"
for i in 1 2; do python3 -m metria run "$S" --no-osv --out "$T/o$i" >/dev/null; sleep 2; done
python3 - "$T" <<'PY'
import json,sys
t=sys.argv[1]; a,b=[json.load(open(f"{t}/o{i}/stress-run.json")) for i in (1,2)]
fp=lambda r: sorted(f["fingerprint"] for l in r["lenses"].values() for f in l["findings"])
print("fingerprints identical:", fp(a)==fp(b))
print("manifest_sha256 identical:", a["evidence_pack"]["manifest_sha256"]==b["evidence_pack"]["manifest_sha256"])
print("evidence.zip sha256 identical:", a["evidence_pack"]["sha256"]==b["evidence_pack"]["sha256"])
PY
