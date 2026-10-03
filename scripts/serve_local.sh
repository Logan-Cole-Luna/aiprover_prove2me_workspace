#!/bin/bash
# Serve an open-weights model through vLLM's OpenAI-compatible API, for use
# as the `openai_compatible` backend of orchestration roles.
#
# Usage: scripts/serve_local.sh [HF_MODEL_ID] [SERVED_NAME] [PORT]
# Tunables (environment): MAX_MODEL_LEN, GPU_MEMORY_UTILIZATION, MAX_NUM_SEQS,
# QUANTIZATION and KV_CACHE_DTYPE (fp8 halves memory per weight and per cached
# token), TOOL_CALL_PARSER (OpenAI tool calls, used by the AIProver harness).
# The server log is written to temp/vllm_<SERVED_NAME>.log.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL_ID="${1:-Qwen/Qwen3-4B-Instruct-2507}"
SERVED_NAME="${2:-qwen3-4b-instruct}"
PORT="${3:-8000}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.84}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-16}"
QUANTIZATION="${QUANTIZATION:-fp8}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-hermes}"

# vLLM's native sampler avoids a FlashInfer kernel that is JIT-compiled at
# startup. The fp8 KV cache uses FlashInfer attention, whose kernels are
# JIT-compiled on first use with ninja and nvcc (CUDA_HOME).
export VLLM_USE_FLASHINFER_SAMPLER=0
export PATH="$ROOT/.venv_serve/bin:/usr/local/cuda/bin:$PATH"
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"

mkdir -p "$ROOT/temp"
exec "$ROOT/.venv_serve/bin/vllm" serve "$MODEL_ID" \
    --served-model-name "$SERVED_NAME" \
    --host 127.0.0.1 --port "$PORT" \
    --dtype bfloat16 \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
    --max-num-seqs "$MAX_NUM_SEQS" \
    --quantization "$QUANTIZATION" \
    --kv-cache-dtype "$KV_CACHE_DTYPE" \
    --enable-auto-tool-choice --tool-call-parser "$TOOL_CALL_PARSER" \
    > "$ROOT/temp/vllm_${SERVED_NAME}.log" 2>&1
