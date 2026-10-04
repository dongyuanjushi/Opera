#!/usr/bin/env bash
# Example: distil Qwen3.5-9B on its own critic-guided trajectories (arm A) and evaluate every checkpoint as the agent.
# Training settings are the reported recipe: 8 GPUs x accumulation 8 (effective batch 64), 3 epochs, lr 2e-6,
# max length 143,360, a checkpoint every 100 steps; evaluation is pass@1 on a held-out SWE-bench Pro split.
#
#   bash src/sft/examples/qwen35_9b.sh data            # 1. dataset from recorded critic runs
#   bash src/sft/examples/qwen35_9b.sh train           # 2. training (detached)
#   bash src/sft/examples/qwen35_9b.sh watch           # 3. evaluate each checkpoint as it is saved (detached)
#   STEP=<step> bash src/sft/examples/qwen35_9b.sh eval    #    or evaluate one checkpoint
#   STEP=<step> bash src/sft/examples/qwen35_9b.sh drift   # 4. drift of a checkpoint vs the base model
#   DRY_RUN=1 bash src/sft/examples/qwen35_9b.sh <stage>   # print the commands only
#
# Needs in .env: SFT_BASE_MODEL = the Qwen3.5-9B weights (local path or hub id), VLLM_VENV = a vLLM environment (eval).
# Fill in every <placeholder> below first.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

NAME=qwen35-9b-armA                     # dataset src/sft/data/<NAME>, run src/sft/runs/<NAME>
TASKS="<training task list>"            # task ids to learn from: a JSON list or one id per line
SOURCE_JOBS=(                           # recorded runs of Qwen3.5-9B under a critic; their passing trajectories on TASKS are the data
  "<critic run dir 1>"
  "<critic run dir 2>"
)
EVAL_SPLIT="<eval split json>"          # held-out tasks ('tasks' + 'cells'); always excluded from the training data
SELECTION="<swebench-pro selection>"    # the swebench-pro selection EVAL_SPLIT's tasks belong to
PROBE="<probe.jsonl>"                   # drift probe; built on first use from the three inputs below
PROBE_SOURCE_JOB="<base-model run dir>" # a recorded no-critic run of the base model (outside TASKS and EVAL_SPLIT)
TRAIN_GPUS=0,1,2,3,4,5,6,7              # NPROC = 8
EVAL_GPUS=0,1                           # vLLM server for the checkpoint under evaluation
LAUNCH=(.venv/bin/python src/sft/launch.py)
DRY=(); [ "${DRY_RUN:-0}" = 1 ] && DRY=(--dry-run)

case "${1:?stage: data | train | watch | eval | drift}" in
  data)
    SRC=(); for j in "${SOURCE_JOBS[@]}"; do SRC+=(--source-job "$j"); done
    "${LAUNCH[@]}" data "${DRY[@]}" --arm A --name "$NAME" --tasks "$TASKS" "${SRC[@]}" --exclude-tasks "$EVAL_SPLIT" ;;
  train)
    "${LAUNCH[@]}" train "${DRY[@]}" --data "$NAME" --run "$NAME" --gpus "$TRAIN_GPUS" \
      --accum 8 --epochs 3 --lr 2e-6 --max-length 143360 --save-steps 100 ;;
  watch)
    "${LAUNCH[@]}" watch "${DRY[@]}" --run "$NAME" --job-prefix "sft-eval-$NAME" --split "$EVAL_SPLIT" --selection "$SELECTION" \
      --gpus "$EVAL_GPUS" --k 1 ;;
  eval)
    "${LAUNCH[@]}" eval "${DRY[@]}" --run "$NAME" --step "${STEP:?STEP=<checkpoint step>}" --job-prefix "sft-eval-$NAME" \
      --split "$EVAL_SPLIT" --selection "$SELECTION" --gpus "$EVAL_GPUS" --k 1 ;;
  drift)
    CKPT=$(ls -d src/sft/runs/"$NAME"/*/checkpoint-"${STEP:?STEP=<checkpoint step>}" 2>/dev/null | head -1 || true)
    "${LAUNCH[@]}" drift "${DRY[@]}" --base "$NAME-$STEP=${CKPT:-src/sft/runs/$NAME/<version>/checkpoint-$STEP}" --gpus 0,1 \
      --probe "$PROBE" --probe-source-job "$PROBE_SOURCE_JOB" --probe-train-tasks "$TASKS" --probe-exclude-tasks "$EVAL_SPLIT" ;;
  *) echo "unknown stage $1 (data | train | watch | eval | drift)" >&2; exit 2 ;;
esac
