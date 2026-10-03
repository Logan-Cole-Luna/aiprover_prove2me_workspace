#!/bin/bash
# Submit the AIProver model server on Vista. Run on a Vista login node from
# the repository root.
#
# Usage: scripts/submit_aiprover_vista.sh MODEL_DIR [gh|gh-dev] [NODES] [TIME]
#   MODEL_DIR  HF-format checkpoint (keep it on /work, TACC.md §2)
#   partition  gh (default) or gh-dev (max 2 h, launch validation)
#   NODES      nodes = tensor-parallel size (default 2)
#   TIME       wall time (default 48:00:00; 02:00:00 on gh-dev)
# Other serve_aiprover_vista.sbatch settings (PORT, QUANTIZATION, ...) are
# taken from the environment; sbatch exports it to the job.
set -euo pipefail

MODEL_DIR=$(readlink -f "${1:?usage: $0 MODEL_DIR [gh|gh-dev] [NODES] [TIME]}")
PARTITION=${2:-gh}
NODES=${3:-2}
if [ "$PARTITION" = "gh-dev" ]; then
    TIME=${4:-02:00:00}
else
    TIME=${4:-48:00:00}
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

[ -f "$MODEL_DIR/config.json" ] || { echo "no config.json in $MODEL_DIR" >&2; exit 1; }
mkdir -p "$SCRATCH/joblogs"
export MODEL_DIR
sbatch -p "$PARTITION" -N "$NODES" -t "$TIME" \
    -o "$SCRATCH/joblogs/%x_%j.out" -e "$SCRATCH/joblogs/%x_%j.err" \
    --export=ALL "$ROOT/scripts/serve_aiprover_vista.sbatch"
echo "handoff: ${HANDOFF:-$SCRATCH/servers/aiprover_server.txt}"
