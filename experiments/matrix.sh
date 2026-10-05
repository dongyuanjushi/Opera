#!/usr/bin/env bash
# Main table: launch every policy x critic cell on one benchmark (detached, logs in experiments/logs/).
#   bash experiments/matrix.sh tb21 3
#   $1        benchmark: tb21 | swebench-pro | deepswe (required)
#   $2        pass@k attempts per task (default 1)
#   POLICIES  agent presets (default: qwen35-9b muse-glimmer-30b qwen38-27b deepseek-v4-flash)
#   CRITICS   critics: none | gpt-5.6 | claude-opus-4-8 | self (default: all four)
#   EXTRA     extra experiments/run.py flags, e.g. --dry-run
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
BENCH="${1:?tb21 | swebench-pro | deepswe}"; K="${2:-1}"
POLICIES="${POLICIES:-qwen35-9b muse-glimmer-30b qwen38-27b deepseek-v4-flash}"
CRITICS="${CRITICS:-none gpt-5.6 claude-opus-4-8 self}"
mkdir -p experiments/logs
for p in $POLICIES; do for c in $CRITICS; do
  log="experiments/logs/${BENCH}-${p}-${c}-k${K}.log"
  echo "== $BENCH $p critic=$c pass@$K -> $log"
  nohup .venv/bin/python experiments/run.py --bench "$BENCH" --policy "$p" --critic "$c" --pass-at-k "$K" ${EXTRA:-} > "$log" 2>&1 &
  sleep 5
done; done
