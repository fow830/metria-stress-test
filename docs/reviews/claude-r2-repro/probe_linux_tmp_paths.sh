#!/usr/bin/env bash
# r2: two identical runs on Linux → MANIFEST differs because /tmp/metria-adeq-* paths are not scrubbed.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
T=$(mktemp -d); S="$T/subj"; mkdir -p "$S"
printf 'def add(a,b): return a+b\n' > "$S/m.py"; printf 'from m import add\ndef test_add(): assert add(1,2)==3\n' > "$S/test_m.py"
git -C "$S" init -q && git -C "$S" -c user.email=p@p -c user.name=p add -A && git -C "$S" -c user.email=p@p -c user.name=p commit -qm i
cd "$REPO"; for i in 1 2; do python3 -m metria run "$S" --no-osv --out "$T/o$i" >/dev/null || true; done
echo "manifest identical: $(cmp -s "$T/o1/evidence/MANIFEST.sha256" "$T/o2/evidence/MANIFEST.sha256" && echo yes || echo NO)"
grep -rhoE '/tmp/metria-[a-z]+-[A-Za-z0-9_]+' "$T/o1/evidence" | sort -u | head -5
