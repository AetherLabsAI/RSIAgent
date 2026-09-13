#!/usr/bin/env bash
# Execute a protocol's three phases with isolated caches and visible logs.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: bash scripts/run_rsi.sh path/to/protocol.json" >&2
  exit 2
fi

RSI_PYTHON="${RSI_PYTHON:-python}"
RSI_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RSI_PROTOCOL="$("$RSI_PYTHON" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' "$1")"
export FORGE_ROOT="$RSI_ROOT"
export OSWORLD_ROOT="${OSWORLD_ROOT:-$(dirname -- "$RSI_ROOT")/OSWorld-V2}"
cd -- "$RSI_ROOT"

# Use the same validator as the orchestrator before creating any run paths.
RSI_NAME="$("$RSI_PYTHON" -c 'from pathlib import Path; import sys; from run_recursive_improvement import load_protocol; print(load_protocol(Path(sys.argv[1]))["run_name"])' "$RSI_PROTOCOL")"
mkdir -p -- "$RSI_ROOT/results/batch_logs/$RSI_NAME"
for RSI_STAGE in 1 2 3; do
  export FORGE_OSWORLD_CACHE_DIR="$RSI_ROOT/results/task_cache/$RSI_NAME/phase$RSI_STAGE"
  "$RSI_PYTHON" -u "$RSI_ROOT/run_recursive_improvement.py" \
    --protocol "$RSI_PROTOCOL" --phase "phase$RSI_STAGE" \
    --execute "RUN-RECURSIVE-IMPROVEMENT-PHASE$RSI_STAGE" \
    2>&1 | tee -a "$RSI_ROOT/results/batch_logs/$RSI_NAME/phase$RSI_STAGE.log"
done
