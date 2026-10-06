"""Run one orchestration on a JiatuBook problem.

Example:
    python -m orchestrator.run --problem-uuid JiatuBook_BoundedArithmetic_000004
    python -m orchestrator.run --run-id <run_id> --resume

A run that stops on an infrastructure failure (a model call failing after all
retries) is resumed automatically up to --max-restarts times. Any interrupted
run, including one that was killed, continues from its last completed step
with --resume.
"""

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import fields
from pathlib import Path

from .claude_cli import get_account_status
from .pipeline import Config, Orchestration

ROOT = Path(__file__).resolve().parent.parent
# JiatuBook validation split, copied from PartitionAndProve
# (`feature/SAM:JiatuBookFormalization/dataset/`).
DATASET = ROOT / "data" / "val_JiatuBook_unlabelled.jsonl"


def load_problem(dataset: Path, uuid: str) -> dict:
    with open(dataset) as f:
        for line in f:
            row = json.loads(line)
            if row["uuid"] == uuid:
                return row
    raise SystemExit(f"{uuid} not found in {dataset}")


def fork_run(source_id: str, run_id: str, stage: str) -> None:
    """Start run `run_id` from the steps of run `source_id` that precede its
    first step in `stage`; the new run then continues with --resume.

    The model calls behind the copied steps are copied into the new run's
    call log, so its totals include the shared prefix.
    """
    source = json.loads((ROOT / "results" / source_id / "trace.json").read_text())
    steps = source["steps"]
    cut = next((i for i, step in enumerate(steps) if step["stage"] == stage), None)
    if cut is None:
        raise SystemExit(f"run {source_id} has no step in stage {stage!r}")
    target = ROOT / "results" / run_id / "trace.json"
    if target.exists():
        raise SystemExit(f"run {run_id} already exists")
    target.parent.mkdir(parents=True)
    forked = {**source, "run_id": run_id, "steps": steps[:cut], "outcome": None,
              "resumptions": [],
              "forked_from": {"run_id": source_id, "stage": stage, "at_step": cut}}
    target.write_text(json.dumps(forked, indent=1, ensure_ascii=False))
    num_calls = sum(step["kind"] == "model_call" for step in steps[:cut])
    calls = (ROOT / "logs" / source_id / "calls.jsonl").read_text().splitlines()[:num_calls]
    (ROOT / "logs" / run_id).mkdir(parents=True, exist_ok=True)
    (ROOT / "logs" / run_id / "calls.jsonl").write_text("".join(c + "\n" for c in calls))


def configure_logging(run_id: str) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    handlers = [logging.StreamHandler(sys.stderr)]
    for path in (ROOT / "logs" / run_id / "run.log", ROOT / "temp" / run_id / "progress.log"):
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path))
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--problem-uuid", default="JiatuBook_BoundedArithmetic_000004")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "config.json")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--resume", action="store_true",
                        help="continue the run given by --run-id from its trace")
    parser.add_argument("--fork-from", default="",
                        help="start --run-id from the steps of this run before --fork-stage")
    parser.add_argument("--fork-stage", default="prove",
                        help="stage at which the forked run diverges (default: prove)")
    parser.add_argument("--max-restarts", type=int, default=2,
                        help="automatic resumptions after infrastructure failures")
    parser.add_argument("--restart-delay", type=float, default=60.0,
                        help="seconds to wait before an automatic resumption")
    scalar_options = [option for option in fields(Config) if option.name != "agents"]
    for option in scalar_options:
        kind = type(option.default)
        parser.add_argument("--" + option.name.replace("_", "-"), default=None,
                            type=(lambda text: text.lower() in ("1", "true", "yes")) if kind is bool
                            else kind)
    args = parser.parse_args()

    settings = json.loads(args.config.read_text())
    for option in scalar_options:
        value = getattr(args, option.name)
        if value is not None:
            settings[option.name] = value
    config = Config(**settings)

    if args.fork_from:
        if not args.run_id:
            parser.error("--fork-from requires --run-id")
        fork_run(args.fork_from, args.run_id, args.fork_stage)
        args.resume = True
    if args.resume:
        if not args.run_id:
            parser.error("--resume requires --run-id")
        trace = json.loads((ROOT / "results" / args.run_id / "trace.json").read_text())
        args.problem_uuid = trace["problem"]["uuid"]
    row = load_problem(args.dataset, args.problem_uuid)
    run_id = args.run_id or f"{time.strftime('%Y%m%d_%H%M%S')}_{row['uuid'].rsplit('_', 1)[-1]}"
    configure_logging(run_id)
    # A terminated process unwinds normally, so the trace records the outcome.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    backends = {spec["backend"] for spec in config.agents.values()}
    if "claude_cli" in backends and get_account_status() is None:
        raise SystemExit("`claude` CLI is not logged in; run `claude login` first.")
    roles = " | ".join(f"{role} {spec['backend']}/{spec.get('model')}"
                       for role, spec in config.agents.items())
    logging.info(f"run {run_id}: {row['uuid']} | {roles} | "
                 f"{config.workers} solver chains per lemma")

    resume = args.resume
    for restart in range(args.max_restarts + 1):
        summary = Orchestration(row, config, run_id, ROOT, resume=resume).run()
        if summary.get("error_kind") != "infrastructure" or restart == args.max_restarts:
            break
        logging.warning(f"infrastructure failure; resuming in {args.restart_delay:.0f}s "
                        f"(restart {restart + 1}/{args.max_restarts})")
        time.sleep(args.restart_delay)
        resume = True
    logging.info(f"status: {summary['status']} | wall {summary['wall_seconds']}s | "
                 f"calls {summary['calls_by_role']}")
    print(json.dumps({k: summary.get(k) for k in
                      ("run_id", "status", "checks", "wall_seconds", "calls_by_role",
                       "usage_by_model")}, indent=2))


if __name__ == "__main__":
    main()
