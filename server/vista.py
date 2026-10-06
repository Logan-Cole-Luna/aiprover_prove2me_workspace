"""AIProver model server jobs on Vista, driven through the ControlMaster.

The query server submits a server job (`scripts/submit_aiprover_vista.sh` on
the login node) when an approved run needs the model and none is queued or
running, and cancels the jobs it submitted once no run needs them.
"""

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AIPROVER_CONFIG = ROOT / "aiprover" / "aiprover_vista.toml"
CONTROL_SOCKET = Path.home() / ".ssh" / "vista.sock"
VISTA_HOST = "vista.tacc.utexas.edu"

# Server job settings; the defaults launch on gh-dev (starts in minutes,
# 2 h limit). A run that outlives its server job resumes on the next one.
PARTITION = os.environ.get("VISTA_PARTITION", "gh-dev")
NODES = os.environ.get("VISTA_NODES", "2")
WALL_TIME = os.environ.get("VISTA_WALL_TIME", "02:00:00")
# AIProver versions a run may select: the trained model (Mistral format, FP8)
# and the base model (HF format, per-expert; `$SCRATCH` expands on Vista).
CHECKPOINTS = {
    "trained": os.environ.get("VISTA_CHECKPOINT", "/work/11428/pjana/aiprover_model"),
    "base": os.environ.get("VISTA_CHECKPOINT_BASE",
                           "$SCRATCH/aiprover_ckpt/leanstral_base_unpacked"),
}
JOB_NAME = "aiprover_srv"  # `#SBATCH -J` in scripts/serve_aiprover_vista.sbatch

logger = logging.getLogger(__name__)


def run_command(arguments: list, timeout: float) -> tuple[int, str]:
    """Run a short command; return (exit code, combined output)."""
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, "AIPROVER_CONFIG": str(AIPROVER_CONFIG)})
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return result.returncode, (result.stdout + result.stderr).strip()


def control_master_running() -> bool:
    code, _ = run_command(["ssh", "-S", str(CONTROL_SOCKET), "-O", "check", VISTA_HOST],
                          timeout=10)
    return code == 0


def remote(command: str, timeout: float = 60) -> tuple[int, str]:
    """Run `command` in a login shell on Vista through the ControlMaster."""
    return run_command(["ssh", "-S", str(CONTROL_SOCKET), "-o", "BatchMode=yes",
                        VISTA_HOST, f"bash -lc {shlex.quote(command)}"], timeout)


def server_jobs() -> dict[str, str] | None:
    """Job id → state of this user's model server jobs; None if Vista is unreachable."""
    code, output = remote(f'squeue -u "$USER" -n {JOB_NAME} -h -o "%i %T"')
    if code != 0:
        return None
    jobs = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit():
            jobs[fields[0]] = fields[1]
    return jobs


def submit_server(checkpoint: str) -> str | None:
    """Submit a model server job for `checkpoint`; return its id, or None on failure."""
    code, output = remote(f"cd $WORK/aiprover_serve && scripts/submit_aiprover_vista.sh "
                          f"{checkpoint} {PARTITION} {NODES} {WALL_TIME}", timeout=120)
    match = re.search(r"Submitted batch job (\d+)", output)
    if code != 0 or not match:
        logger.error(f"Vista submission failed: {output[-500:]}")
        return None
    logger.info(f"submitted Vista server job {match.group(1)} for {checkpoint} "
                f"({PARTITION}, {NODES} nodes, {WALL_TIME})")
    return match.group(1)


def cancel_server(job_id: str) -> bool:
    code, output = remote(f"scancel {job_id}")
    if code != 0:
        logger.error(f"scancel {job_id} failed: {output[-300:]}")
    return code == 0
