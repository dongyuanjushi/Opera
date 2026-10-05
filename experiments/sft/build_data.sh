#!/usr/bin/env bash
# Build a per-turn policy SFT set from recorded runs (src/sft/launch.py data).
#   EVAL_SPLIT=<eval split json> bash experiments/sft/build_data.sh <A|B|C> <name> <task list> <job dir>...
# Args: A|B|C = source arm (`src/sft/launch.py data --help`); name = src/sft/data/<name>; task list = JSON list or one id per line.
# Env: EVAL_SPLIT (required) = eval split whose tasks never enter training.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
ARM="${1:?A|B|C}"; NAME="${2:?dataset name}"; TASKS="${3:?task list}"; shift 3
SRC=(); for j in "$@"; do SRC+=(--source-job "$j"); done
exec .venv/bin/python src/sft/launch.py data --arm "$ARM" --name "$NAME" --tasks "$TASKS" "${SRC[@]}" \
  --exclude-tasks "${EVAL_SPLIT:?set EVAL_SPLIT}"
