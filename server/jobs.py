"""Job store and queue worker for the query server.

Each job is one orchestration run, executed as a subprocess of the existing
entry point (`python -m orchestrator.run`). A submitted job awaits approval;
approved jobs start in submission order, up to MAX_CONCURRENT_RUNS at once
(environment QUERY_SERVER_MAX_RUNS). Concurrent runs share one model server;
two runs of the same problem never overlap, since a run's Lean modules in
`prove2me_workspace/` are named after its theorem.

The worker keeps the model server in step with demand: it submits a Vista
server job when an approved run is waiting and none is queued or running,
and cancels the server jobs it submitted after IDLE_SECONDS without work. A
run that loses its model server stops as an infrastructure failure and is
put back at the head of the queue, to resume on the next server job.

Runs are started in their own session, so a server restart does not end
them; on start, the worker adopts a run whose process is still alive and
resumes (`--resume`) one whose process is gone.
"""

import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import models, vista
from .vista import AIPROVER_CONFIG, run_command

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "server" / "state"
DATABASE = STATE_DIR / "jobs.db"
PROBLEM_DIR = STATE_DIR / "problems"
DATASET = ROOT / "data" / "val_JiatuBook_unlabelled.jsonl"
# Every JSONL file in data/ is offered as a source of problems.
DATA_DIR = ROOT / "data"
CONFIG = ROOT / "orchestrator" / "config_aiprover_vista_served.json"
AIPROVER_CLI = ROOT / "AIProver" / "AIProver_plugin" / "bin" / "aiprover"

POLL_SECONDS = 5
BACKEND_RETRY_SECONDS = 60
IDLE_SECONDS = 15 * 60
MAX_CONCURRENT_RUNS = int(os.environ.get("QUERY_SERVER_MAX_RUNS", "2"))
# A run that loses its model server is requeued and resumed without limit;
# it ends only after this many resumptions in a row with no progress.
MAX_STALLED_RESUMPTIONS = 3
PROGRESS_EVENTS = ("formalization_compiled", "audit_verdict", "sketch_accepted",
                   "lemma_proved", "replan")
OPEN_STATES = ("awaiting_approval", "queued", "running", "cancelling")
# Per-run options the worker uses itself rather than passing to run.py:
# `after` names a run that must end before this one starts (a run that
# builds on a library entry the other adds).
SERVER_OPTIONS = ("after",)

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    run_id      TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    uuid        TEXT NOT NULL,
    title       TEXT NOT NULL,
    dataset     TEXT NOT NULL,
    -- awaiting_approval, queued, running, cancelling, finished, cancelled, rejected
    state       TEXT NOT NULL,
    status      TEXT,           -- summary.json status of a finished run
    pid         INTEGER,
    submitted   REAL NOT NULL,
    started     REAL,
    ended       REAL,
    approved_by TEXT,
    resumptions INTEGER NOT NULL DEFAULT 0,
    progress    INTEGER NOT NULL DEFAULT 0,  -- progress events at the last requeue
    stalled     INTEGER NOT NULL DEFAULT 0,  -- requeues in a row without progress
    options     TEXT,                        -- JSON {config field: value} for this run
    agents      TEXT                         -- JSON models of the run, see models.py
);
-- Vista server jobs submitted by the worker (only these are cancelled when idle).
CREATE TABLE IF NOT EXISTS vista_jobs (
    job_id      TEXT PRIMARY KEY,
    submitted   REAL NOT NULL,
    checkpoint  TEXT                         -- checkpoint the job serves
);
"""
# Columns added after the first deployment, for databases created before them.
MIGRATIONS = ["ALTER TABLE jobs ADD COLUMN approved_by TEXT",
              "ALTER TABLE jobs ADD COLUMN resumptions INTEGER NOT NULL DEFAULT 0",
              "ALTER TABLE jobs ADD COLUMN progress INTEGER NOT NULL DEFAULT 0",
              "ALTER TABLE jobs ADD COLUMN stalled INTEGER NOT NULL DEFAULT 0",
              "ALTER TABLE jobs ADD COLUMN options TEXT",
              "ALTER TABLE jobs ADD COLUMN agents TEXT",
              "ALTER TABLE vista_jobs ADD COLUMN checkpoint TEXT"]


REASONING_PROXY = "http://127.0.0.1:18565/v1/models"


def proxy_up() -> bool:
    """True if the reasoning proxy (server/reasoning_proxy.py) forwards to the model."""
    try:
        with urllib.request.urlopen(REASONING_PROXY, timeout=20) as reply:
            return reply.status == 200
    except OSError:
        return False


def endpoint_status(bring_up: bool) -> tuple[bool, str]:
    """Probe the AIProver endpoint; with `bring_up`, open the tunnel first.
    Served runs reach the model through the reasoning proxy, so it is
    required as well."""
    action = "up" if bring_up else "status"
    code, output = run_command([str(AIPROVER_CLI), "tunnel", action],
                               timeout=180 if bring_up else 20)
    message = output.splitlines()[-1] if output else ""
    if code == 0 and not proxy_up():
        return False, "reasoning proxy not answering on 127.0.0.1:18565"
    return code == 0, message


def job_selection(job: dict) -> dict:
    """Models chosen for a job; empty for a job submitted before the choice
    existed, which runs with the served config."""
    return json.loads(job.get("agents") or "null") or {}


def job_checkpoint(job: dict) -> str | None:
    """Checkpoint the model server must serve for `job`; None if it needs none.
    The served config's solver uses the trained model."""
    selection = job_selection(job)
    return models.checkpoint_of(selection) if selection else vista.CHECKPOINTS["trained"]


def process_alive(pid: int | None, run_id: str) -> bool:
    """True if `pid` is a live orchestrator process of run `run_id`."""
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z" and run_id.encode() in cmdline


def read_summary(run_id: str) -> dict:
    path = ROOT / "results" / run_id / "summary.json"
    return json.loads(path.read_text()) if path.exists() else {}


def progress_count(run_id: str) -> int:
    """Durable steps of a run (decisions a resume restores) in its trace."""
    path = ROOT / "results" / run_id / "trace.json"
    try:
        steps = json.loads(path.read_text()).get("steps", [])
    except (OSError, json.JSONDecodeError):
        return 0
    return sum(step.get("event") in PROGRESS_EVENTS for step in steps)


class JobStore:
    """SQLite-backed job records; safe to share between threads."""

    def __init__(self, path: Path = DATABASE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._connection.executescript(SCHEMA)
            for statement in MIGRATIONS:
                try:
                    self._connection.execute(statement)
                except sqlite3.OperationalError:
                    pass  # column exists
            self._connection.commit()

    def _execute(self, query: str, parameters: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
            self._connection.commit()
        return [dict(row) for row in rows]

    def add(self, job: dict) -> None:
        columns = ", ".join(job)
        placeholders = ", ".join("?" for _ in job)
        self._execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
                      tuple(job.values()))

    def update(self, run_id: str, **values) -> None:
        assignments = ", ".join(f"{column} = ?" for column in values)
        self._execute(f"UPDATE jobs SET {assignments} WHERE run_id = ?",
                      (*values.values(), run_id))

    def get(self, run_id: str) -> dict | None:
        rows = self._execute("SELECT * FROM jobs WHERE run_id = ?", (run_id,))
        return rows[0] if rows else None

    def jobs(self, owner: str | None = None) -> list[dict]:
        if owner is None:
            return self._execute("SELECT * FROM jobs ORDER BY submitted DESC")
        return self._execute("SELECT * FROM jobs WHERE owner = ? ORDER BY submitted DESC",
                             (owner,))

    def in_states(self, *states: str) -> list[dict]:
        marks = ", ".join("?" for _ in states)
        return self._execute(f"SELECT * FROM jobs WHERE state IN ({marks}) "
                             "ORDER BY submitted", states)

    def queue_position(self, run_id: str) -> int | None:
        """1-based position among queued jobs, or None if not queued."""
        queued = [job["run_id"] for job in self.in_states("queued")]
        return queued.index(run_id) + 1 if run_id in queued else None

    def managed_vista_jobs(self) -> list[str]:
        return [row["job_id"] for row in self._execute("SELECT job_id FROM vista_jobs")]

    def vista_job_checkpoints(self) -> dict[str, str | None]:
        rows = self._execute("SELECT job_id, checkpoint FROM vista_jobs")
        return {row["job_id"]: row["checkpoint"] for row in rows}

    def add_vista_job(self, job_id: str, checkpoint: str) -> None:
        self._execute("INSERT OR IGNORE INTO vista_jobs (job_id, submitted, checkpoint) "
                      "VALUES (?, ?, ?)", (job_id, time.time(), checkpoint))

    def remove_vista_job(self, job_id: str) -> None:
        self._execute("DELETE FROM vista_jobs WHERE job_id = ?", (job_id,))


class Worker(threading.Thread):
    """Runs approved jobs, up to MAX_CONCURRENT_RUNS at once, and manages the
    Vista model server."""

    def __init__(self, store: JobStore):
        super().__init__(name="job-worker", daemon=True)
        self.store = store
        # Run id -> its orchestrator process; None for a run adopted after a
        # server restart, which is followed by its pid.
        self.running: dict[str, subprocess.Popen | None] = {}
        self.vista_message = "not checked"
        self.idle_since: float | None = None
        # Checkpoint the model endpoint was last confirmed to serve.
        self.serving: str | None = None

    def run(self) -> None:
        for job in self.store.in_states("running", "cancelling"):
            self._recover(job)
        while True:
            try:
                self._step()
            except Exception:
                logger.exception("worker step failed")
                time.sleep(BACKEND_RETRY_SECONDS)

    def _step(self) -> None:
        self._reap()
        queued = self.store.in_states("queued")
        if not queued and not self.running:
            self._release_idle_servers()
            time.sleep(POLL_SECONDS)
            return
        self.idle_since = None
        candidate = self._next_to_start(queued)
        if candidate is None:
            time.sleep(POLL_SECONDS)
            return
        checkpoint = job_checkpoint(candidate)
        if checkpoint is None or self._serving(checkpoint):
            self._execute(candidate)
            return
        time.sleep(BACKEND_RETRY_SECONDS)

    def _next_to_start(self, queued: list[dict]) -> dict | None:
        """The first queued job that may start now: a slot is free, no run of
        the same problem is in progress, and the model server's checkpoint is
        the one the job needs or may change. Running jobs share one checkpoint;
        a job waiting for another one holds back later jobs that use a model
        server, so that it is not starved."""
        if len(self.running) >= MAX_CONCURRENT_RUNS:
            return None
        running = [self.store.get(run_id) for run_id in self.running]
        busy = {job["uuid"] for job in running}
        in_use = {job_checkpoint(job) for job in running} - {None}
        held_back = False
        for job in queued:
            if job["uuid"] in busy or self._waiting_for_dependency(job):
                continue
            checkpoint = job_checkpoint(job)
            if checkpoint is None:
                return job
            if held_back:
                continue
            if not in_use or checkpoint in in_use:
                return job
            held_back = True
        return None

    def _waiting_for_dependency(self, job: dict) -> bool:
        after = json.loads(job.get("options") or "{}").get("after")
        dependency = self.store.get(after) if after else None
        return dependency is not None and dependency["state"] in OPEN_STATES

    def _reap(self) -> None:
        """Finish the runs whose process has exited."""
        for run_id, process in list(self.running.items()):
            if process is not None:
                done = process.poll() is not None
            else:
                done = not process_alive(self.store.get(run_id)["pid"], run_id)
            if done:
                del self.running[run_id]
                self._finish(self.store.get(run_id))

    # Vista server ---------------------------------------------------------

    def _serving(self, checkpoint: str) -> bool:
        """True if the model endpoint is up and serves `checkpoint`; otherwise
        bring the server in line with it: replace a server job of another
        checkpoint (none of the running jobs uses it), or submit one."""
        if self.serving == checkpoint:
            endpoint_up, message = endpoint_status(bring_up=True)
            if endpoint_up:
                self.vista_message = message
                return True
            self.serving = None
        jobs = vista.server_jobs()
        if jobs is None:
            self.vista_message = ("Vista unreachable: reopen the ControlMaster "
                                  "(~/.ssh/vista.sock) with MFA")
            logger.warning(self.vista_message)
            return False
        checkpoints = self.store.vista_job_checkpoints()
        for job_id in checkpoints:
            if job_id not in jobs:
                self.store.remove_vista_job(job_id)
        # A job of an earlier deployment has no recorded checkpoint: the default.
        serves = {job_id: checkpoints.get(job_id) or vista.CHECKPOINTS["trained"]
                  for job_id in jobs}
        for job_id in [job_id for job_id, served in serves.items() if served != checkpoint]:
            if job_id not in checkpoints:
                self.vista_message = f"waiting for server job {job_id}, which serves another checkpoint"
                return False
            logger.info(f"cancelling server job {job_id} (serves {serves[job_id]}) "
                        f"for a run that needs {checkpoint}")
            if vista.cancel_server(job_id):
                self.store.remove_vista_job(job_id)
            del serves[job_id]
        if not serves:
            job_id = vista.submit_server(checkpoint)
            if job_id:
                self.store.add_vista_job(job_id, checkpoint)
                self.vista_message = f"submitted server job {job_id}"
            else:
                self.vista_message = "server job submission failed (see server log)"
            return False
        endpoint_up, message = endpoint_status(bring_up=True)
        if endpoint_up:
            # A further job of this checkpoint would take over the handoff file
            # when it starts and leave this one idle, so pending ones are cancelled.
            if any(state == "RUNNING" for state in jobs.values()):
                for job_id, state in jobs.items():
                    if state == "PENDING" and job_id in checkpoints and vista.cancel_server(job_id):
                        logger.info(f"cancelled pending server job {job_id}; another serves {checkpoint}")
                        self.store.remove_vista_job(job_id)
            self.serving = checkpoint
            self.vista_message = message
            return True
        summary = ", ".join(f"{job_id} {state}" for job_id, state in jobs.items())
        self.vista_message = f"waiting for server job {summary}"
        return False

    def _release_idle_servers(self) -> None:
        """Cancel the server jobs this worker submitted after IDLE_SECONDS without work."""
        managed = self.store.managed_vista_jobs()
        if not managed or self.store.in_states("running", "cancelling"):
            self.idle_since = None
            return
        self.idle_since = self.idle_since or time.time()
        if time.time() - self.idle_since < IDLE_SECONDS:
            return
        for job_id in managed:
            logger.info(f"no queued runs for {IDLE_SECONDS // 60} min; cancelling {job_id}")
            if vista.cancel_server(job_id):
                self.store.remove_vista_job(job_id)
        self.serving = None
        self.vista_message = "server jobs cancelled after idle period"
        self.idle_since = None

    # Runs -----------------------------------------------------------------

    def _recover(self, job: dict) -> None:
        """Finish a job left running by a previous server process."""
        if process_alive(job["pid"], job["run_id"]):
            logger.info(f"adopting running job {job['run_id']} (pid {job['pid']})")
            self.running[job["run_id"]] = None
        elif job["state"] == "cancelling" or self._completed_while_away(job):
            self._finish(job)
        else:
            logger.info(f"requeueing interrupted job {job['run_id']}")
            self.store.update(job["run_id"], state="queued", pid=None)

    @staticmethod
    def _completed_while_away(job: dict) -> bool:
        """True if the run wrote a final summary after the server stopped
        watching it; an interrupted run's summary does not count."""
        summary_path = ROOT / "results" / job["run_id"] / "summary.json"
        if not summary_path.exists() or summary_path.stat().st_mtime < (job["started"] or 0):
            return False
        return read_summary(job["run_id"]).get("error_kind") != "interrupted"

    def _execute(self, job: dict) -> None:
        run_id = job["run_id"]
        # Restarts after infrastructure failures are made by the worker, which
        # waits for a new model server; run.py's own restarts would not.
        config_path = ROOT / "temp" / f"{run_id}.config.json"
        config_path.parent.mkdir(exist_ok=True)
        config_path.write_text(json.dumps(models.build_config(CONFIG, job_selection(job)),
                                          indent=1))
        arguments = [sys.executable, "-m", "orchestrator.run",
                     "--dataset", job["dataset"], "--config", str(config_path),
                     "--run-id", run_id, "--max-restarts", "0"]
        # Per-run settings over the served config, e.g. more attempts per lemma.
        for name, value in json.loads(job.get("options") or "{}").items():
            if name in SERVER_OPTIONS:
                continue
            arguments += ["--" + name.replace("_", "-"), str(value)]
        if (ROOT / "results" / run_id / "trace.json").exists():
            arguments.append("--resume")
        else:
            arguments += ["--problem-uuid", job["uuid"]]
        output_path = ROOT / "temp" / f"{run_id}.out"
        output_path.parent.mkdir(exist_ok=True)
        with open(output_path, "a") as output:
            process = subprocess.Popen(
                arguments, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONPATH": str(ROOT),
                     "AIPROVER_CONFIG": str(AIPROVER_CONFIG)},
                start_new_session=True)
        self.running[run_id] = process
        self.store.update(run_id, state="running", pid=process.pid,
                          started=job["started"] or time.time())
        logger.info(f"started {run_id} (pid {process.pid}); "
                    f"{len(self.running)}/{MAX_CONCURRENT_RUNS} runs in progress")

    def _finish(self, job: dict) -> None:
        run_id = job["run_id"]
        summary = read_summary(run_id)
        if job["state"] != "cancelling" and summary.get("error_kind") == "infrastructure":
            progress = progress_count(run_id)
            stalled = job["stalled"] + 1 if progress <= job["progress"] else 0
            if stalled < MAX_STALLED_RESUMPTIONS:
                logger.info(f"{run_id}: infrastructure failure; requeued to resume "
                            f"(resumption {job['resumptions'] + 1}"
                            f"{f', {stalled} without progress' if stalled else ''})")
                self.store.update(run_id, state="queued", pid=None, progress=progress,
                                  stalled=stalled, resumptions=job["resumptions"] + 1)
                return
            logger.info(f"{run_id}: {stalled} resumptions in a row without progress; ended")
        trace_path = ROOT / "results" / run_id / "trace.json"
        if trace_path.exists():
            run_command([sys.executable, "-m", "orchestrator.trace_view", str(trace_path)],
                        timeout=300)
        state = "cancelled" if job["state"] == "cancelling" else "finished"
        status = summary.get("status") or "no_summary"
        self.store.update(run_id, state=state, status=status, pid=None, ended=time.time())
        logger.info(f"{run_id}: {state} ({status})")

    def approve(self, run_id: str, approver: str) -> bool:
        job = self.store.get(run_id)
        if job is None or job["state"] != "awaiting_approval":
            return False
        self.store.update(run_id, state="queued", approved_by=approver)
        logger.info(f"{run_id} approved by {approver}")
        return True

    def reject(self, run_id: str, approver: str) -> bool:
        job = self.store.get(run_id)
        if job is None or job["state"] != "awaiting_approval":
            return False
        self.store.update(run_id, state="rejected", approved_by=approver, ended=time.time())
        logger.info(f"{run_id} rejected by {approver}")
        return True

    def cancel(self, run_id: str) -> bool:
        """Withdraw or dequeue a job, or stop a running one; False if it is not open."""
        job = self.store.get(run_id)
        if job is None or job["state"] not in ("awaiting_approval", "queued", "running"):
            return False
        if job["state"] != "running":
            self.store.update(run_id, state="cancelled", ended=time.time())
            return True
        self.store.update(run_id, state="cancelling")
        # SIGTERM to the orchestrator only: it unwinds, cancels its AIProver
        # jobs and records the interruption in the trace.
        try:
            os.kill(job["pid"], signal.SIGTERM)
        except (OSError, TypeError):
            pass
        return True
