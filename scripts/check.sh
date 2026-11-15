#!/bin/sh
# Crow OS full local check: unit tests + every self-contained probe harness.
# Usage: scripts/check.sh [python]
set -e
PY="${1:-python3}"
cd "$(dirname "$0")/.."
echo "== unit tests"
$PY -m unittest discover -s tests
for h in probes/edge_harness.py probes/edge_harness2.py probes/edge_harness3.py probes/edge_harness4.py probes/edge_harness5.py probes/edge_harness6.py probes/edge_harness7.py probes/edge_harness8.py probes/edge_harness9.py probes/edge_harness10.py probes/edge_harness11.py probes/edge_harness12.py probes/edge_harness13.py probes/edge_harness14.py probes/stub_harness.py; do
  echo "== $h"
  $PY "$h" 2>/dev/null | tail -1
done
echo "ALL CHECKS DONE"
