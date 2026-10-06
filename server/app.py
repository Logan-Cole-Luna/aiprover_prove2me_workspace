"""Web server for submitting problems to the orchestration and following runs.

Start (single process; the job worker lives in it):
    uvicorn server.app:app --host 0.0.0.0 --port 8443

Users authenticate with a bearer token from `server/tokens.json`
(`{"<token>": "<user name>"}`), sent as `Authorization: Bearer <token>` or
as the `token` cookie that `POST /api/login` sets for the web page.
With `QUERY_SERVER_AUTH=off` in the environment, no token is required and
every visitor sees and may cancel every run (trusted testing only).

A submission awaits approval; any token holder approves or rejects it,
whether or not tokens are required for the other operations.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import vista
from .jobs import (DATA_DIR, OPEN_STATES, PROBLEM_DIR, ROOT, JobStore, Worker,
                   endpoint_status)
from .vista import control_master_running

TOKENS = ROOT / "server" / "tokens.json"
AUTH_REQUIRED = os.environ.get("QUERY_SERVER_AUTH", "on") != "off"
GUEST = "guest"
INDEX_PAGE = ROOT / "server" / "static" / "index.html"
MAX_OPEN_JOBS_PER_USER = 3
MAX_QUEUED_JOBS = 20
MAX_STATEMENT_CHARS = 20_000
MAX_PROOF_CHARS = 50_000
RESULT_PAGES = ("trace.html", "trace_replay.html", "summary.json")
TERMINAL_STATES = ("finished", "cancelled", "rejected")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
store = JobStore()
worker = Worker(store)


@asynccontextmanager
async def lifespan(_: FastAPI):
    worker.start()
    yield


app = FastAPI(title="AIProver orchestration", lifespan=lifespan)


# ── Authentication ─────────────────────────────────────────────────────────

_token_cache: tuple[float, dict] = (0.0, {})


def load_tokens() -> dict[str, str]:
    """Token → user name; re-read when the file changes, so no restart is needed."""
    global _token_cache
    try:
        modified = TOKENS.stat().st_mtime
    except OSError:
        return {}
    if modified != _token_cache[0]:
        try:
            tokens = json.loads(TOKENS.read_text() or "{}")
        except json.JSONDecodeError:
            logging.error(f"{TOKENS} is not valid JSON; no token is accepted")
            tokens = {}
        _token_cache = (modified, tokens)
    return _token_cache[1]


def user_for_token(token: str) -> str | None:
    for known, user in load_tokens().items():
        if secrets.compare_digest(token.encode(), known.encode()):
            return user
    return None


def token_user(request: Request) -> str | None:
    """User of the request's token (header or cookie), or None."""
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else request.cookies.get("token", "")
    return user_for_token(token) if token else None


def current_user(request: Request) -> str:
    user = token_user(request)
    if user is None and not AUTH_REQUIRED:
        return GUEST
    if user is None:
        raise HTTPException(401, "missing or unknown token")
    return user


def approver(request: Request) -> str:
    user = token_user(request)
    if user is None:
        raise HTTPException(403, "approval requires a token")
    return user


def visible_owner(user: str) -> str | None:
    """Owner whose runs `user` sees; None (all runs) without authentication."""
    return user if AUTH_REQUIRED else None


def owned_job(run_id: str, user: str = Depends(current_user)) -> dict:
    job = store.get(run_id)
    if job is None or visible_owner(user) not in (None, job["owner"]):
        raise HTTPException(404, "no such run")
    return job


class Login(BaseModel):
    token: str


@app.post("/api/login")
def login(body: Login, request: Request, response: Response) -> dict:
    user = user_for_token(body.token)
    if user is None:
        raise HTTPException(401, "unknown token")
    response.set_cookie("token", body.token, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=30 * 86400)
    return {"user": user}


@app.post("/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie("token")
    return {}


# ── Problems and runs ──────────────────────────────────────────────────────

def book_label(statement: str) -> str:
    match = re.search(r"book label `([^`]+)`", statement)
    return match.group(1) if match else ""


def read_dataset() -> dict[str, dict]:
    """Problems of every JSONL file in data/, by UUID, each with its file."""
    problems = {}
    for path in sorted(DATA_DIR.glob("*.jsonl")):
        with open(path) as f:
            for line in f:
                row = json.loads(line)
                problems[row["uuid"]] = row | {"_dataset": str(path)}
    return problems


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_PAGE)


@app.get("/api/me")
def me(request: Request, user: str = Depends(current_user)) -> dict:
    return {"user": user, "auth_required": AUTH_REQUIRED,
            "approver": token_user(request) is not None}


@app.get("/api/health")
def health(user: str = Depends(current_user)) -> dict:
    endpoint_up, endpoint_message = endpoint_status(bring_up=False)
    running = store.in_states("running", "cancelling")
    server_jobs = vista.server_jobs()
    return {"control_master": control_master_running(),
            "endpoint_up": endpoint_up,
            "endpoint": endpoint_message,
            "vista_jobs": server_jobs,
            "vista": worker.vista_message,
            "awaiting_approval": len(store.in_states("awaiting_approval")),
            "queued": len(store.in_states("queued")),
            "running": running[0]["run_id"] if running else None}


@app.get("/api/problems")
def problems(user: str = Depends(current_user)) -> list[dict]:
    return [{"uuid": uuid, "domain": row.get("domain"), "source": row.get("source_name"),
             "label": book_label(row.get("informal_statement", ""))}
            for uuid, row in read_dataset().items()]


class Submission(BaseModel):
    uuid: str = ""
    statement: str = ""
    proof: str = ""


def new_run_id(suffix: str) -> str:
    base = f"srv_{time.strftime('%Y%m%d_%H%M%S')}_{suffix}"
    run_id, count = base, 1
    while store.get(run_id) or (ROOT / "results" / run_id).exists():
        count += 1
        run_id = f"{base}_{count}"
    return run_id


@app.post("/api/runs", status_code=201)
def submit(body: Submission, user: str = Depends(current_user)) -> dict:
    open_jobs = [job for job in store.jobs(user) if job["state"] in OPEN_STATES]
    if AUTH_REQUIRED and len(open_jobs) >= MAX_OPEN_JOBS_PER_USER:
        raise HTTPException(429, f"at most {MAX_OPEN_JOBS_PER_USER} open runs per user")
    if len(store.in_states("awaiting_approval", "queued")) >= MAX_QUEUED_JOBS:
        raise HTTPException(503, "queue is full")

    if body.uuid:
        row = read_dataset().get(body.uuid)
        if row is None:
            raise HTTPException(400, f"unknown problem {body.uuid}")
        # JiatuBook ids end in a problem number; other datasets use the whole id.
        suffix = body.uuid.rsplit("_", 1)[-1] if body.uuid.startswith("JiatuBook_") else body.uuid
        run_id = new_run_id(suffix)
        uuid, dataset = body.uuid, row["_dataset"]
        title = book_label(row["informal_statement"]) or body.uuid
    else:
        statement, proof = body.statement.strip(), body.proof.strip()
        if not statement:
            raise HTTPException(400, "give a dataset uuid or an informal statement")
        if len(statement) > MAX_STATEMENT_CHARS or len(proof) > MAX_PROOF_CHARS:
            raise HTTPException(413, "statement or proof too long")
        run_id = new_run_id("user")
        uuid = f"user_{run_id}"
        dataset = PROBLEM_DIR / f"{run_id}.jsonl"
        dataset.parent.mkdir(parents=True, exist_ok=True)
        row = {"uuid": uuid, "source_name": "user", "domain": "user",
               "informal_statement": statement,
               "informal_proof": proof or "(No informal proof is given.)"}
        dataset.write_text(json.dumps(row) + "\n")
        title = statement.splitlines()[0][:120]

    store.add({"run_id": run_id, "owner": user, "uuid": uuid, "title": title,
               "dataset": str(dataset), "state": "awaiting_approval",
               "submitted": time.time()})
    return {"run_id": run_id, "state": "awaiting_approval"}


def describe(job: dict) -> dict:
    result_dir = ROOT / "results" / job["run_id"]
    files = [name for name in RESULT_PAGES if (result_dir / name).exists()]
    if any(result_dir.glob("*_standalone.lean")):
        files.append("standalone.lean")
    return {key: job[key] for key in ("run_id", "owner", "uuid", "title", "state", "status",
                                      "submitted", "started", "ended", "approved_by",
                                      "resumptions")} | {
        "position": store.queue_position(job["run_id"]), "files": files}


@app.get("/api/runs")
def list_runs(user: str = Depends(current_user)) -> list[dict]:
    return [describe(job) for job in store.jobs(visible_owner(user))]


@app.get("/api/runs/{run_id}")
def get_run(job: dict = Depends(owned_job)) -> dict:
    summary_path = ROOT / "results" / job["run_id"] / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else None
    return describe(job) | {"summary": summary}


@app.delete("/api/runs/{run_id}")
def cancel_run(job: dict = Depends(owned_job)) -> dict:
    if not worker.cancel(job["run_id"]):
        raise HTTPException(409, f"run is {job['state']}")
    return describe(store.get(job["run_id"]))


@app.post("/api/runs/{run_id}/approve")
def approve_run(run_id: str, user: str = Depends(approver)) -> dict:
    if not worker.approve(run_id, user):
        raise HTTPException(409, "run is not awaiting approval")
    return describe(store.get(run_id))


@app.post("/api/runs/{run_id}/reject")
def reject_run(run_id: str, user: str = Depends(approver)) -> dict:
    if not worker.reject(run_id, user):
        raise HTTPException(409, "run is not awaiting approval")
    return describe(store.get(run_id))


@app.get("/api/runs/{run_id}/files/{name}")
def get_file(name: str, job: dict = Depends(owned_job)) -> FileResponse:
    result_dir = ROOT / "results" / job["run_id"]
    if name == "standalone.lean":
        matches = sorted(result_dir.glob("*_standalone.lean"))
        path = matches[0] if matches else None
    else:
        path = result_dir / name if name in RESULT_PAGES else None
    if path is None or not path.exists():
        raise HTTPException(404, "no such file")
    media_type = "text/plain; charset=utf-8" if path.suffix == ".lean" else None
    return FileResponse(path, media_type=media_type)


@app.get("/api/runs/{run_id}/progress")
async def progress(job: dict = Depends(owned_job)) -> StreamingResponse:
    """Server-sent events: the run's progress log, line by line, then `end`."""
    run_id = job["run_id"]
    log_path = ROOT / "temp" / run_id / "progress.log"

    async def events():
        offset = 0
        while True:
            if log_path.exists():
                with open(log_path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                # A line still being written is sent once it is complete.
                complete = chunk[:chunk.rfind(b"\n") + 1]
                offset += len(complete)
                for line in complete.decode(errors="replace").splitlines():
                    yield f"data: {json.dumps(line)}\n\n"
            state = store.get(run_id)["state"]
            if state in TERMINAL_STATES:
                yield f"event: end\ndata: {json.dumps(state)}\n\n"
                return
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
