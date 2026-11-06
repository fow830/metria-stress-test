#!/usr/bin/env bash
# N: `metria run <local path> --sha X` runs `git checkout X` inside the operator's own working copy.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
T=$(mktemp -d); S="$T/subj"; mkdir -p "$S"
cd "$S"; git init -q; echo a > f.py; git -c user.email=p@p -c user.name=p add -A; git -c user.email=p@p -c user.name=p commit -qm a
FIRST=$(git rev-parse HEAD); echo b > f.py; git -c user.email=p@p -c user.name=p commit -qam b
echo "before: $(git rev-parse --abbrev-ref HEAD)"
cd "$REPO"; python3 -m metria run "$S" --sha "$FIRST" --no-osv --no-tests --out "$T/o" >/dev/null || true
echo "after:  $(git -C "$S" rev-parse --abbrev-ref HEAD)  (detached = operator checkout was moved)"
