#!/usr/bin/env bash
# Full-parameter policy SFT, detached (src/sft/launch.py train).
#   bash experiments/sft/train.sh <dataset under src/sft/data> <run name>
# Env (default): GPUS (0,1,2,3); ACCUM (8; effective batch = #GPUs x ACCUM); EPOCHS (3); LR (2e-6);
#   MAX_LENGTH (143360); SAVE_STEPS (100); MASTER_PORT (29815). Needs SFT_BASE_MODEL in .env.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec .venv/bin/python src/sft/launch.py train --data "${1:?dataset}" --run "${2:?run name}" --gpus "${GPUS:-0,1,2,3}" \
  --accum "${ACCUM:-8}" --epochs "${EPOCHS:-3}" --lr "${LR:-2e-6}" --max-length "${MAX_LENGTH:-143360}" \
  --save-steps "${SAVE_STEPS:-100}" --master-port "${MASTER_PORT:-29815}"
