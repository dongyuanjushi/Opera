#!/usr/bin/env bash
# Supervised fine-tuning with ms-swift (`swift sft`) on an ms-swift messages dataset; checkpoints every SAVE_STEPS.
# Data: one JSONL of {"messages": [...]} rows, already shuffled, no validation split.
#   DATA=... MODEL=... OUT=... bash src/sft/pipeline/train_critic.sh [extra swift sft flags]
# ms-swift is vendor/ms-swift (v4.4.1), installed editable into .venv/sft (see vendor/README.md).
#
# Env (default):
#   MODEL         base weights to fine-tune, local path or hub id (required)
#   DATA          train.jsonl (required)
#   OUT           output dir; checkpoints land in OUT/.../checkpoint-<step> (required)
#   MAX_LENGTH    max tokens per example (131072)
#   SAVE_STEPS    checkpoint interval in optimizer steps (50)
#   EPOCHS / LR   epochs (2) / learning rate (1e-5)
#   BATCH / ACCUM per-device batch (1) / gradient accumulation (8); effective batch = NPROC x BATCH x ACCUM
#   NPROC         GPUs per node (8)
#   TUNER_TYPE    full | lora (full)
#   DEEPSPEED     zero2 | zero3 | "" to disable (zero3)
#   ATTN          sdpa | flash_attn (sdpa)
#   PADDING_FREE  true | false; true needs flash_attn (false)
#   LOSS_SCALE    last_round = supervise only the final assistant turn | default = all assistant turns (last_round)
#   AGENT_TEMPLATE  ms-swift agent template for tool calls (qwen3_5)
#   TRUNCATION    left = keep the tail of over-long examples | delete = drop them (left)
#   LIGER         use the Liger fused kernels (true)
#   EXTRA_ARGS    extra `swift sft` flags (empty)
#   SWIFT         swift executable (.venv/sft/bin/swift)
#   KEEP_LD_LIBRARY_PATH  1 = keep LD_LIBRARY_PATH (0: unset it, an external cudnn breaks torch's own)
set -euo pipefail
OPERA="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$OPERA"

MODEL="${MODEL:?set MODEL (base weights)}"
DATA="${DATA:?set DATA (train.jsonl)}"
OUT="${OUT:?set OUT (output dir)}"
MAX_LENGTH="${MAX_LENGTH:-131072}"
SAVE_STEPS="${SAVE_STEPS:-50}"
EPOCHS="${EPOCHS:-2}"
LR="${LR:-1e-5}"
BATCH="${BATCH:-1}"
ACCUM="${ACCUM:-8}"
NPROC="${NPROC:-8}"
TUNER_TYPE="${TUNER_TYPE:-full}"
DEEPSPEED="${DEEPSPEED:-zero3}"
ATTN="${ATTN:-sdpa}"
PADDING_FREE="${PADDING_FREE:-false}"
LOSS_SCALE="${LOSS_SCALE:-last_round}"
AGENT_TEMPLATE="${AGENT_TEMPLATE:-qwen3_5}"
TRUNCATION="${TRUNCATION:-left}"
LIGER="${LIGER:-true}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

SWIFT="${SWIFT:-$OPERA/.venv/sft/bin/swift}"
[ "${KEEP_LD_LIBRARY_PATH:-0}" = "1" ] || unset LD_LIBRARY_PATH
export NPROC_PER_NODE="$NPROC"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
[ -n "$DEEPSPEED" ] && set -- --deepspeed "$DEEPSPEED" "$@"
mkdir -p "$OUT"

# --truncation_strategy is always passed: ms-swift's own default (delete) silently drops every example over max_length.
"$SWIFT" sft \
  --model "$MODEL" \
  --dataset "$DATA" \
  --split_dataset_ratio 0 \
  --max_length "$MAX_LENGTH" \
  --truncation_strategy "$TRUNCATION" \
  --loss_scale "$LOSS_SCALE" \
  --agent_template "$AGENT_TEMPLATE" \
  --torch_dtype bfloat16 \
  --num_train_epochs "$EPOCHS" \
  --warmup_ratio 0.03 \
  --save_only_model true \
  --use_liger_kernel "$LIGER" \
  --learning_rate "$LR" \
  --per_device_train_batch_size "$BATCH" \
  --gradient_accumulation_steps "$ACCUM" \
  --gradient_checkpointing true \
  --tuner_type "$TUNER_TYPE" \
  --attn_impl "$ATTN" \
  --padding_free "$PADDING_FREE" \
  --save_steps "$SAVE_STEPS" \
  --save_total_limit 20 \
  --logging_steps 5 \
  --output_dir "$OUT" \
  --dataloader_num_workers 4 \
  $EXTRA_ARGS \
  "$@"
