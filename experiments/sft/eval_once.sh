#!/usr/bin/env bash
# Evaluate one checkpoint as the agent: serve, run, stop the server; resumable (src/sft/launch.py eval).
#   EVAL_SPLIT=<eval split json> SELECTION=<selection> bash experiments/sft/eval_once.sh <run name> <step> <job prefix>
# Env: EVAL_SPLIT, SELECTION (required); GPUS = server GPUs (0,1); PORT (30903); K = attempts per task, pass@k (1).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec .venv/bin/python src/sft/launch.py eval --run "${1:?run name}" --step "${2:?step}" --job-prefix "${3:?job prefix}" \
  --gpus "${GPUS:-0,1}" --port "${PORT:-30903}" --k "${K:-1}" \
  --split "${EVAL_SPLIT:?set EVAL_SPLIT}" --selection "${SELECTION:?set SELECTION}"
