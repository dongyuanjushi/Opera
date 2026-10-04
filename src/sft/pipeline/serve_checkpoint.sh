#!/usr/bin/env bash
# Serve one checkpoint with vLLM under the model id `sft-critic` (the sft-critic preset); prints the
# SFT_CRITIC_BASE_URL to export. A LoRA checkpoint is served as an adapter on SFT_BASE_MODEL.
#   bash src/sft/pipeline/serve_checkpoint.sh <checkpoint-dir> [port=30900]
#
# Env (default; .env is loaded first):
#   VLLM_VENV        venv whose bin/ holds vllm and ninja (required)
#   SFT_BASE_MODEL   base weights for a LoRA checkpoint (required; BASE_MODEL overrides)
#   GPUS             CUDA devices, comma separated (0,1,2,3); TP = tensor parallel size (number of GPUS)
#   MAX_LEN          --max-model-len (262144)
#   SERVED_NAME      model id for a full checkpoint (sft-critic)
#   MAX_LORA_RANK    --max-lora-rank for a LoRA checkpoint (32)
#   TOOL_CALL_PARSER / REASONING_PARSER  vLLM parsers (qwen3_coder / qwen3)
#   VLLM_EXTRA_ARGS  extra `vllm serve` flags (empty)
set -euo pipefail
OPERA="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if [ -f "$OPERA/.env" ]; then set -a; . "$OPERA/.env"; set +a; fi
CKPT="${1:?usage: serve_checkpoint.sh <checkpoint-dir> [port]}"
PORT="${2:-30900}"
MAX_LEN="${MAX_LEN:-262144}"
GPUS="${GPUS:-0,1,2,3}"
TP="${TP:-$(awk -F, '{print NF}' <<<"$GPUS")}"
VLLM_VENV="${VLLM_VENV:?set VLLM_VENV in .env (the venv that has vllm installed)}"
BASE_MODEL="${BASE_MODEL:-${SFT_BASE_MODEL:?set SFT_BASE_MODEL in .env (the base model path)}}"
# flashinfer's JIT calls a bare `ninja`, so the vLLM venv must be on PATH
export PATH="$VLLM_VENV/bin:$PATH"

if [ -f "$CKPT/adapter_config.json" ]; then
  ARGS=(serve "$BASE_MODEL" --served-model-name sft-critic-base
        --enable-lora --max-lora-rank "${MAX_LORA_RANK:-32}" --lora-modules "sft-critic=$CKPT")
else
  ARGS=(serve "$CKPT" --served-model-name "${SERVED_NAME:-sft-critic}")
fi

# The tool-call / reasoning parsers are required: OpenHands sends tool_choice="auto", which vLLM rejects without them.
CUDA_VISIBLE_DEVICES="$GPUS" "$VLLM_VENV/bin/vllm" "${ARGS[@]}" \
  --host 0.0.0.0 \
  --port "$PORT" \
  --trust-remote-code \
  --tensor-parallel-size "$TP" \
  --enable-auto-tool-choice \
  --tool-call-parser "${TOOL_CALL_PARSER:-qwen3_coder}" \
  --reasoning-parser "${REASONING_PARSER:-qwen3}" \
  --max-model-len "$MAX_LEN" ${VLLM_EXTRA_ARGS:-} &
echo "serving $CKPT on :$PORT — export SFT_CRITIC_BASE_URL=http://$(hostname -I | awk '{print $1}'):$PORT/v1"
wait
