#!/bin/bash -l
#SBATCH -J aiprover_srv
#SBATCH -p gh
#SBATCH -N 2
#SBATCH --ntasks-per-node=1
#SBATCH -c 64
#SBATCH -t 48:00:00
#SBATCH -A CDA25016
#SBATCH -o aiprover_srv_%j.out
#SBATCH -e aiprover_srv_%j.err
#
# AIProver model server on TACC Vista (GH200, one GPU per node).
#
# One Ray cluster spans the allocated nodes; `vllm serve` runs on the head
# node with tensor parallelism equal to the node count (default 2). The
# OpenAI-compatible API listens on the head node at $PORT. Clients locate the
# server through $HANDOFF (`node=<n> port=<p> job=<id> model=<name>`), written
# before the weights load; readiness is established by polling /v1/models
# (TACC.md §10). The orchestration VM reaches the port through an SSH forward
# on the vista.sock ControlMaster (aiprover/aiprover_vista.toml).
#
# Submit from a Vista login node (scripts/submit_aiprover_vista.sh):
#   MODEL_DIR=/work/.../aiprover_ckpt sbatch scripts/serve_aiprover_vista.sbatch
#
# Memory. HBM is 95 GiB per node and is carved out of the node's 212 GiB
# (TACC.md §7). A bf16 Leanstral-class checkpoint (~222 GiB) needs ~111 GiB
# per rank at TP=2, which exceeds HBM. Two remedies are configurable:
#   CPU_OFFLOAD_GB  GiB of weights per rank held in Grace memory and read over
#                   NVLink-C2C (default: computed from the checkpoint size so
#                   that WEIGHT_HBM_GIB of weights stay resident per GPU).
#   QUANTIZATION    e.g. "fp8": online weight quantization (~56 GiB per rank,
#                   no offload, numerics differ from the measured bf16 model).
# Three or more nodes (SBATCH -N) serve bf16 without offload.
#
# Environment (all optional except MODEL_DIR):
#   MODEL_DIR           HF-format checkpoint directory (required)
#   SERVED_NAME         served model id                   [aiprover]
#   PORT                API port on the head node         [8060]
#   HANDOFF             handoff file                      [$SCRATCH/servers/aiprover_server.txt]
#   SIF                 aarch64 vLLM image                [$WORK/containers/vllm-gh200.sif]
#   MAX_MODEL_LEN       context length                    [262144]
#   GPU_MEMORY_UTILIZATION                                [0.90]
#   WEIGHT_HBM_GIB      resident weights per GPU (auto offload target) [70]
#   ATTENTION_BACKEND   vLLM attention backend            [vLLM default]
#   TOKENIZER_MODE      mistral (tekken.json, no tokenizer.json) [mistral]
#   CPU_OFFLOAD_GB, QUANTIZATION, KV_CACHE_DTYPE, ENFORCE_EAGER [1], EXTRA_ARGS
set -uo pipefail
set +u; module load tacc-apptainer/1.4.1; set -u

: "${MODEL_DIR:?set MODEL_DIR to the AIProver checkpoint directory}"
SERVED_NAME=${SERVED_NAME:-aiprover}
PORT=${PORT:-8060}
RAY_PORT=${RAY_PORT:-6379}
HANDOFF=${HANDOFF:-$SCRATCH/servers/aiprover_server.txt}
SIF=${SIF:-$WORK/containers/vllm-gh200.sif}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-262144}
GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.90}
WEIGHT_HBM_GIB=${WEIGHT_HBM_GIB:-70}
QUANTIZATION=${QUANTIZATION:-}
KV_CACHE_DTYPE=${KV_CACHE_DTYPE:-auto}
ENFORCE_EAGER=${ENFORCE_EAGER:-1}
TOKENIZER_MODE=${TOKENIZER_MODE:-mistral}
EXTRA_ARGS=${EXTRA_ARGS:-}
JOBLOGS=$SCRATCH/joblogs
mkdir -p "$(dirname "$HANDOFF")" "$JOBLOGS"

[ -f "$SIF" ] || { echo "ERROR: container $SIF not found" >&2; exit 1; }
[ -f "$MODEL_DIR/config.json" ] || { echo "ERROR: $MODEL_DIR/config.json not found" >&2; exit 1; }

# Triton locates libcuda through Apptainer's bind mount, and compiles with
# the container's gcc rather than a host compiler on PATH.
export TRITON_LIBCUDA_PATH=/.singularity.d/libs
export CC=/usr/bin/gcc CXX=/usr/bin/g++
# Compute nodes have no egress; the checkpoint is read from disk only.
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export TMPDIR=/tmp/$USER
mkdir -p "$TMPDIR"

nodes=($(scontrol show hostnames "$SLURM_JOB_NODELIST"))
num_nodes=${#nodes[@]}
head_node=${nodes[0]}
head_ip=$(hostname -i | awk '{print $1}')

# Weights per rank and the offload needed to keep WEIGHT_HBM_GIB resident.
ckpt_gib=$(du -sb --apparent-size "$MODEL_DIR" | awk '{printf "%.1f", $1 / 2^30}')
if [ -z "${CPU_OFFLOAD_GB:-}" ]; then
  if [ -n "$QUANTIZATION" ]; then
    CPU_OFFLOAD_GB=0
  else
    CPU_OFFLOAD_GB=$(awk -v c="$ckpt_gib" -v n="$num_nodes" -v h="$WEIGHT_HBM_GIB" \
      'BEGIN { o = c / n - h; print (o > 0) ? int(o + 1) : 0 }')
  fi
fi

echo "node=$head_node port=$PORT job=$SLURM_JOB_ID model=$SERVED_NAME" \
     "nodes=$num_nodes tp=$num_nodes started=$(date -Is)" | tee "$HANDOFF"
echo "checkpoint=$MODEL_DIR size_gib=$ckpt_gib per_rank_gib=$(awk -v c="$ckpt_gib" -v n="$num_nodes" 'BEGIN{printf "%.1f", c/n}')" \
     "cpu_offload_gb=$CPU_OFFLOAD_GB quantization=${QUANTIZATION:-none}"

# Ray state lives in the host /tmp, which persists between jobs.
for node in "${nodes[@]}"; do
  srun --nodes=1 --ntasks=1 -w "$node" apptainer exec --nv "$SIF" ray stop --force >/dev/null 2>&1 || true
done

BIND="--bind $MODEL_DIR"
apptainer exec --nv $BIND "$SIF" python3 -c \
  'import vllm, torch; print("vllm", vllm.__version__, "torch", torch.__version__)' \
  | tee -a "$HANDOFF"
# The head runs in this batch step (no srun) so that its task slot remains free.
apptainer exec --nv $BIND "$SIF" \
  ray start --head --node-ip-address="$head_ip" --port="$RAY_PORT" --num-gpus 1 --block &
sleep 15
for ((i = 1; i < num_nodes; i++)); do
  srun --nodes=1 --ntasks=1 -w "${nodes[$i]}" \
    apptainer exec --nv $BIND "$SIF" \
    ray start --address "$head_ip:$RAY_PORT" --num-gpus 1 --block &
  sleep 5
done
sleep 20

serve_args=(
  "$MODEL_DIR"
  --served-model-name "$SERVED_NAME"
  --host 0.0.0.0 --port "$PORT"
  --tensor-parallel-size "$num_nodes"
  --distributed-executor-backend ray
  --max-model-len "$MAX_MODEL_LEN"
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
  --kv-cache-dtype "$KV_CACHE_DTYPE"
  --tool-call-parser mistral --enable-auto-tool-choice
  --reasoning-parser mistral
  --tokenizer-mode "$TOKENIZER_MODE"
  --config-format hf --load-format safetensors
  # Mistral3 carries a Pixtral vision encoder; the harness sends text only.
  --limit-mm-per-prompt '{"image": 0}'
)
[ -n "${ATTENTION_BACKEND:-}" ] && serve_args+=(--attention-backend "$ATTENTION_BACKEND")
[ "$CPU_OFFLOAD_GB" != "0" ] && serve_args+=(--cpu-offload-gb "$CPU_OFFLOAD_GB")
[ -n "$QUANTIZATION" ] && serve_args+=(--quantization "$QUANTIZATION")
[ "$ENFORCE_EAGER" = "1" ] && serve_args+=(--enforce-eager)

# The server is restarted inside the allocation if it exits, so that a crash
# does not cost a new queue wait (TACC.md §1, §8).
attempt=0
while [ "$attempt" -lt "${MAX_ATTEMPTS:-5}" ]; do
  attempt=$((attempt + 1))
  log=$JOBLOGS/vllm_aiprover_${SLURM_JOB_ID}_a${attempt}.log
  echo "attempt=$attempt log=$log $(date -Is)" | tee -a "$HANDOFF"
  apptainer exec --nv $BIND "$SIF" env RAY_ADDRESS="$head_ip:$RAY_PORT" \
    vllm serve "${serve_args[@]}" $EXTRA_ARGS > "$log" 2>&1
  echo "vllm exited rc=$? $(date -Is)"
  sleep 30
done
