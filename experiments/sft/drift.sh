#!/usr/bin/env bash
# Policy drift (KL / NLL / entropy vs the base policy on held-out turns), one GPU per checkpoint (src/sft/launch.py drift).
#   PROBE=<probe.jsonl> bash experiments/sft/drift.sh <label>=<checkpoint dir> ...
# Env: PROBE (required) = probe JSONL; if it does not exist it is built from PROBE_SOURCE_JOB (a base-model run),
#      skipping TRAIN_TASKS and the tasks of EVAL_SPLIT; BASE = 1 also probes the base model.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
BUILD=()
[ -n "${PROBE_SOURCE_JOB:-}" ] && BUILD+=(--probe-source-job "$PROBE_SOURCE_JOB")
[ -n "${TRAIN_TASKS:-}" ] && BUILD+=(--probe-train-tasks "$TRAIN_TASKS")
[ -n "${EVAL_SPLIT:-}" ] && BUILD+=(--probe-exclude-tasks "$EVAL_SPLIT")
exec .venv/bin/python src/sft/launch.py drift "$@" --probe "${PROBE:?set PROBE}" "${BUILD[@]}" $([ "${BASE:-0}" = 1 ] && echo --base)
