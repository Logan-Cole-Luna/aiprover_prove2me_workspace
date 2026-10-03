#!/bin/bash -l
#SBATCH -J aiprover_unpack
#SBATCH -p gg
#SBATCH -N 1
#SBATCH --ntasks-per-node=1
#SBATCH -t 04:00:00
#SBATCH -A CDA25016
#
# Fused-expert checkpoint -> per-expert checkpoint, for vLLM's native loader.
#
# The SAM/transformers checkpoints store each layer's routed experts as two
# packed tensors, `experts.gate_up_proj` [E, 2I, H] and `experts.down_proj`
# [E, H, I]. vLLM's deepseek_v2 loader maps only per-expert names
# (`experts.<e>.{gate,up,down}_proj.weight`) and re-fuses them itself. The
# layout follows transformers' modeling_deepseek_v3.py: each slice is an
# [out, in] linear weight, and rows [0:I] of gate_up are the gate projection.
# Shards are cut only at layer boundaries so that transformers, which re-packs
# experts shard by shard, can also load the result. config.json, the
# tokenizer and every other non-weight file are copied unchanged.
#
# CPU only (Grace-Grace `gg` node). Usage, from a Vista login node:
#   sbatch --export=ALL,SRC=<fused ckpt>,DST=<output dir> \
#       scripts/unpack_experts_vista.sbatch
set -uo pipefail
set +u; module load tacc-apptainer/1.4.1; set -u

: "${SRC:?set SRC to the fused checkpoint directory}"
: "${DST:?set DST to the output directory}"
SIF=${SIF:-$WORK/containers/vllm-gh200.sif}
SHARD_GIB=${SHARD_GIB:-5}
mkdir -p "$DST"
echo "src=$SRC dst=$DST $(date -Is)"

apptainer exec --bind "$SRC" --bind "$DST" "$SIF" \
    python3 -u - "$SRC" "$DST" "$SHARD_GIB" <<'PY'
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from safetensors import safe_open
from safetensors.torch import save_file

src, dst, shard_gib = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
config = json.loads((src / "config.json").read_text())
text_config = config.get("text_config", config)
num_experts = int(text_config["n_routed_experts"])
intermediate = int(text_config["moe_intermediate_size"])
hidden = int(text_config["hidden_size"])

index_path = src / "model.safetensors.index.json"
if index_path.exists():
    index = json.loads(index_path.read_text())
    shards = sorted(set(index["weight_map"].values()))
else:
    index, shards = {"metadata": {}}, ["model.safetensors"]

shard_limit = int(shard_gib * 2**30)
weight_map, buffer = {}, {}
buffer_bytes, shard_count, packed_count = 0, 0, 0
current_layer = "start"
start = time.time()


def flush():
    global buffer, buffer_bytes, shard_count
    if not buffer:
        return
    shard_count += 1
    name = f"model-{shard_count:05d}-of-XXXXX.safetensors"
    save_file(buffer, str(dst / name), metadata={"format": "pt"})
    for key in buffer:
        weight_map[key] = name
    print(f"wrote {name}: {len(buffer)} tensors, "
          f"{buffer_bytes / 2**30:.1f} GiB, {time.time() - start:.0f} s",
          flush=True)
    buffer, buffer_bytes = {}, 0


def layer_of(key):
    match = re.search(r"\.layers\.(\d+)\.", key)
    return int(match.group(1)) if match else None


def put(key, tensor):
    """Buffer a tensor; a shard is written only when the layer changes."""
    global buffer_bytes, current_layer
    layer = layer_of(key)
    if layer != current_layer:
        if buffer_bytes >= shard_limit:
            flush()
        current_layer = layer
    buffer[key] = tensor.contiguous()
    buffer_bytes += tensor.numel() * tensor.element_size()


for shard in shards:
    with safe_open(str(src / shard), framework="pt", device="cpu") as f:
        for key in f.keys():
            if key.endswith(".mlp.experts.gate_up_proj"):
                weight = f.get_tensor(key)
                expected = (num_experts, 2 * intermediate, hidden)
                assert tuple(weight.shape) == expected, (key, weight.shape)
                prefix = key[: -len("gate_up_proj")]
                for e in range(num_experts):
                    put(f"{prefix}{e}.gate_proj.weight",
                        weight[e, :intermediate, :].clone())
                    put(f"{prefix}{e}.up_proj.weight",
                        weight[e, intermediate:, :].clone())
                packed_count += 1
            elif key.endswith(".mlp.experts.down_proj"):
                weight = f.get_tensor(key)
                expected = (num_experts, hidden, intermediate)
                assert tuple(weight.shape) == expected, (key, weight.shape)
                prefix = key[: -len("down_proj")]
                for e in range(num_experts):
                    put(f"{prefix}{e}.down_proj.weight", weight[e].clone())
                packed_count += 1
            else:
                put(key, f.get_tensor(key))
    print(f"{shard} done, {time.time() - start:.0f} s", flush=True)
flush()

# Shard names carry the total count, known only now.
final_map = {}
for key, name in weight_map.items():
    number = int(name.split("-")[1])
    final_map[key] = f"model-{number:05d}-of-{shard_count:05d}.safetensors"
for number in range(1, shard_count + 1):
    os.rename(dst / f"model-{number:05d}-of-XXXXX.safetensors",
              dst / f"model-{number:05d}-of-{shard_count:05d}.safetensors")
(dst / "model.safetensors.index.json").write_text(json.dumps(
    {"metadata": index.get("metadata", {}), "weight_map": final_map},
    indent=2) + "\n")

for path in src.iterdir():
    is_weight = (path.name.endswith(".safetensors")
                 or path.name == "model.safetensors.index.json")
    if path.is_file() and not is_weight:
        shutil.copy2(path, dst / path.name)

expert_keys = [k for k in final_map if ".mlp.experts." in k]
packed_left = [k for k in final_map
               if k.endswith(("experts.gate_up_proj", "experts.down_proj"))]
layers = int(text_config["num_hidden_layers"])
print(f"packed tensors expanded: {packed_count} (expected {2 * layers})")
print(f"per-expert tensors: {len(expert_keys)} "
      f"(expected {layers * num_experts * 3}); packed left: {len(packed_left)}")
print(f"shards: {shard_count}; elapsed {time.time() - start:.0f} s")
assert packed_count == 2 * layers and not packed_left
assert len(expert_keys) == layers * num_experts * 3
print("UNPACK OK")
PY
rc=$?
echo "unpack exit=$rc $(date -Is)"
exit $rc
