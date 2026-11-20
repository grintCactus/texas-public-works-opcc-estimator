#!/usr/bin/env bash
# Full rebuild, in the README's order, one heavy step at a time.
#
# Why a script: the steps must run in this order, and the run stops at the
# first step that fails rather than building later outputs on top of a broken
# one.  provenance_check.py, train_test_guard.py and company_twin_scan.py exit
# non-zero on a train/test leak, so a leak stops the run too.
#
# Usage (bash, Git Bash or macOS):  bash src/rebuild_all.sh [--from <script-name>]
#   PYTHON=/path/to/python overrides the interpreter
#     (default: the project venv .venv/bin/python, else python, else python3)
#   --from parse_bidtabs   skip every step before src/parse_bidtabs.py
#   BIDTAB_WORKERS=n       PDF parser processes (default 8)
# Log:               reports/measurements/rebuild_<timestamp>.log
#
# Machine: since 2026-11-19 the project runs on a MacBook (M5 Pro, 18 cores,
# 48 GB).  The old Windows PC blue-screened under memory load (bad RAM), which
# is why this script used to hold the parser to 2 workers; that limit was the
# PC's, not the code's.
#
# Parser pool: the 2026-11-19 06:11 run on the PC hung -- with 2 workers
# recycled every 20 files, exactly 40 files got parsed, both workers exited to
# be replaced, and the parent waited an hour with no error.  Worker recycling
# is off by default in parse_bidtabs.py (BIDTAB_TASKS_PER_CHILD unset).
set -u
cd "$(dirname "$0")/.."
# The interpreter: the project venv has the pinned packages; plain python3 on
# the Mac is the bare python.org build without them.
if [ -z "${PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python
  elif command -v python >/dev/null 2>&1; then PYTHON=python; else PYTHON=python3; fi
fi
export PYTHONIOENCODING=utf-8 BIDTAB_WORKERS="${BIDTAB_WORKERS:-8}" OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
# pdftotext (poppler) is read by audit_external, index_external, provenance_check
# and pick_extractor.  The PC had it on PATH; on the Mac it lives in tools/poppler
# (see tools/README.txt).  Without it those steps fall back to other extractors
# WITHOUT an error and the classifications drift, so a missing pdftotext stops here.
if [ -x tools/poppler/bin/pdftotext ]; then export PATH="$PWD/tools/poppler/bin:$PATH"; fi
if ! command -v pdftotext >/dev/null 2>&1; then
  echo "STOPPED: pdftotext not found -- install poppler (see tools/README.txt)"; exit 1
fi
FROM=""
if [ "${1:-}" = "--from" ]; then FROM="${2:-}"; fi
mkdir -p reports/measurements
LOG="reports/measurements/rebuild_$(date +%Y%m%d_%H%M).log"

run() {
  if [ -n "$FROM" ]; then
    case "$*" in
      *"$FROM"*) FROM="" ;;
      *) echo "=== skip $*" | tee -a "$LOG"; return 0 ;;
    esac
  fi
  echo "=== $(date +%H:%M:%S) $*" | tee -a "$LOG"
  "$@" >> "$LOG" 2>&1
  rc=$?
  echo "=== $(date +%H:%M:%S) exit $rc" | tee -a "$LOG"
  if [ $rc -ne 0 ]; then
    echo "STOPPED at: $*" | tee -a "$LOG"
    exit $rc
  fi
}

# 0. inputs: no public copy of a company letting may be where the parser reads
run "$PYTHON" src/company_twin_scan.py
# 1. external corpus bookkeeping (moves out-of-window files; never run beside a harvest)
run "$PYTHON" src/audit_external.py
# 1b. image-only scans: OCR the ones still unreadable (cached sidecars, so only
#     new scans are read), re-audit so they classify from that text, and scan
#     for company twins again -- the scan now reads the sidecars; before, a
#     scanned public copy of a company tab was checked on its file name alone
run "$PYTHON" src/ocr_scans.py
run "$PYTHON" src/audit_external.py
run "$PYTHON" src/company_twin_scan.py
run "$PYTHON" src/index_external.py
run "$PYTHON" src/quarantine_pre2020.py
# 2. parse (PARSER_VERSION was bumped, so every file is re-parsed)
run "$PYTHON" src/parse_bidtabs.py
run "$PYTHON" src/qc_report.py
# 3. other loaders (they re-derive item_family from the current rules)
run "$PYTHON" src/load_txdot.py
run "$PYTHON" src/parse_txdot_archive.py
run "$PYTHON" src/parse_garland_avg.py
run "$PYTHON" src/parse_dwu_manual.py
run "$PYTHON" src/load_erosion_statewide.py
# 4. panel, awards, discount, index, estimator calibration
run "$PYTHON" src/build_project_metadata.py
run "$PYTHON" src/build_vocabulary.py
run "$PYTHON" src/award_identity.py
run "$PYTHON" src/build_master.py
run "$PYTHON" src/award_identity.py
run "$PYTHON" src/competition_and_crosswalk.py
run "$PYTHON" src/index_by_market.py
run "$PYTHON" src/estimate_low_bid.py
run "$PYTHON" src/estimate_accuracy_benchmark.py
run "$PYTHON" src/opcc_analysis.py
# 5. train/test guards: non-zero exit means a real leak
run "$PYTHON" src/provenance_check.py
run "$PYTHON" src/train_test_guard.py
echo "=== $(date +%H:%M:%S) DONE" | tee -a "$LOG"
