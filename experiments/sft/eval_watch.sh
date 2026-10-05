#!/usr/bin/env bash
# Evaluate each new checkpoint of a run as the agent, detached (src/sft/launch.py watch).
#   EVAL_SPLIT=<eval split json> SELECTION=<selection> bash experiments/sft/eval_watch.sh <run name> <job prefix>
# Env: EVAL_SPLIT, SELECTION (required) = eval split and its swebench-pro selection; GPUS = server GPUs (0,1);
#      PORT (30900); K = attempts per task, pass@k (1). Needs VLLM_VENV in .env.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec .venv/bin/python src/sft/launch.py watch --run "${1:?run name}" --job-prefix "${2:?job prefix}" \
  --gpus "${GPUS:-0,1}" --port "${PORT:-30900}" --k "${K:-1}" \
  --split "${EVAL_SPLIT:?set EVAL_SPLIT}" --selection "${SELECTION:?set SELECTION}"
