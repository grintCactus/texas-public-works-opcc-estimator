#!/usr/bin/env bash
# Before a deploy: prove that the assembled package (deploy/space) prices the
# same as this checkout.
#
#     bash deploy/tools/check_package.sh [N]        # N schedules, default 400
#
# 1. deploy/tools/identity_sweep.py writes N public lettings as schedules
#    (each priced as a new job on its own bid date);
# 2. prices them in this checkout and in a copy of deploy/space running in
#    public mode;
# 3. compares every number of every result.
#
# Passes only when P50, P70 and P80 are identical in all N and no other value
# differs. Three string fields may differ by design: local_path and local_kind
# (links to documents on this machine, which the public package does not
# have) and engine/data (the modification time of master_panel.csv).
# Run deploy/tools/assemble_space.py first. Takes about three minutes.
set -euo pipefail
N="${1:-400}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SPACE="${OPCC_PACKAGE_DIR:-$ROOT/deploy/space}"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3
T="$(mktemp -d "${TMPDIR:-/tmp}/opcc_check.XXXXXX")"
trap 'rm -rf "$T"' EXIT
[ -f "$SPACE/Dockerfile" ] || { echo "STOP: package is missing -- run deploy/tools/assemble_space.py"; exit 2; }

echo "start $(date '+%H:%M:%S')"
cp -Rp "$SPACE" "$T/space"
"$PY" "$ROOT/deploy/tools/identity_sweep.py" make "$T/sweep.json" --n "$N"
"$PY" "$ROOT/deploy/tools/identity_sweep.py" run "$ROOT" "$T/sweep.json" "$T/checkout.json"
(cd "$T" && "$PY" "$ROOT/deploy/tools/identity_sweep.py" run "$T/space" "$T/sweep.json" "$T/package.json" --public)
rc=0
"$PY" "$ROOT/deploy/tools/identity_sweep.py" compare "$T/checkout.json" "$T/package.json" > "$T/compare.txt" || rc=$?
tail -8 "$T/compare.txt"
echo "done $(date '+%H:%M:%S')"
if [ "$rc" -ne 0 ] || ! grep -q "P50, P70 and P80 identical in $N\$" "$T/compare.txt"; then
  echo "RESULT: NOT IDENTICAL -- do not deploy"; exit 1
fi
echo "RESULT: IDENTICAL in $N schedules"
