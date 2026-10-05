#!/usr/bin/env bash
# Ablations for one policy on one benchmark with the gpt-5.6 critic (detached, logs in experiments/logs/).
#   bash experiments/ablations.sh tb21 qwen38-27b 1
#   $1         benchmark: tb21 | swebench-pro | deepswe (required)
#   $2         agent preset (required)
#   $3         pass@k attempts per task (default 1)
#   ABLATIONS  none (full Opera control) | edit | noedit | noaudit (default: all four)
#   EXTRA      extra experiments/run.py flags, e.g. --dry-run
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
BENCH="${1:?tb21 | swebench-pro | deepswe}"; POLICY="${2:?agent preset}"; K="${3:-1}"
ABLATIONS="${ABLATIONS:-none edit noedit noaudit}"
mkdir -p experiments/logs
for ab in $ABLATIONS; do
  log="experiments/logs/${BENCH}-${POLICY}-gpt-5.6-${ab}-k${K}.log"
  echo "== $BENCH $POLICY ablation=$ab pass@$K -> $log"
  nohup .venv/bin/python experiments/run.py --bench "$BENCH" --policy "$POLICY" --critic gpt-5.6 --ablation "$ab" --pass-at-k "$K" ${EXTRA:-} > "$log" 2>&1 &
  sleep 5
done
