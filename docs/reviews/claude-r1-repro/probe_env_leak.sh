#!/usr/bin/env bash
# B1: subject conftest.py executes on the host with the operator's full environment.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
T=$(mktemp -d); S="$T/subj"; mkdir -p "$S/tests"
cat > "$S/tests/conftest.py" <<'PY'
import os, pathlib
pathlib.Path(os.environ["PROBE_SINK"]).write_text(
    "\n".join(k for k in sorted(os.environ) if "TOKEN" in k or "KEY" in k or "SECRET" in k))
PY
echo 'def test_x(): assert True' > "$S/tests/test_x.py"
git -C "$S" init -q && git -C "$S" -c user.email=p@p -c user.name=p add -A && git -C "$S" -c user.email=p@p -c user.name=p commit -qm i
export PROBE_SINK="$T/leaked.txt" FAKE_GITHUB_TOKEN=ghp_should_not_reach_subject
cd "$REPO" && python3 -m metria run "$S" --no-osv --no-tests --out "$T/out" >/dev/null || true
echo "--- env var names visible to subject code (run with --no-tests!):"; cat "$T/leaked.txt"; echo
