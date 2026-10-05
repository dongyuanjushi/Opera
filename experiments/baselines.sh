#!/usr/bin/env bash
# Baseline critic strategies for one policy on one benchmark (detached, logs in experiments/logs/).
#   bash experiments/baselines.sh swebench-pro qwen38-27b 1
#   $1          benchmark: tb21 | swebench-pro | deepswe (required)
#   $2          agent preset (required)
#   $3          pass@k attempts per task (default 1)
#   STRATEGIES  swe_prm | swe_search | llm_verifier | agentic_rubrics (default: all four)
#   CRITIC      critic preset (default gpt-5.6)
#   EXTRA       extra experiments/run.py flags, e.g. --dry-run
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
BENCH="${1:?tb21 | swebench-pro | deepswe}"; POLICY="${2:?agent preset}"; K="${3:-1}"
STRATEGIES="${STRATEGIES:-swe_prm swe_search llm_verifier agentic_rubrics}"
mkdir -p experiments/logs
for s in $STRATEGIES; do
  log="experiments/logs/${BENCH}-${POLICY}-${CRITIC:-gpt-5.6}-${s}-k${K}.log"
  echo "== $BENCH $POLICY strategy=$s pass@$K -> $log"
  nohup .venv/bin/python experiments/run.py --bench "$BENCH" --policy "$POLICY" --critic "${CRITIC:-gpt-5.6}" --strategy "$s" --pass-at-k "$K" ${EXTRA:-} > "$log" 2>&1 &
  sleep 5
done
