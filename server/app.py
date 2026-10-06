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
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

from orchestrator import library as libraries

from . import models, vista
from .jobs import (DATA_DIR, OPEN_STATES, PROBLEM_DIR, ROOT, JobStore, Worker,
                   endpoint_status, job_selection)
from .vista import control_master_running

TOKENS = ROOT / "server" / "tokens.json"
AUTH_REQUIRED = os.environ.get("QUERY_SERVER_AUTH", "on") != "off"
GUEST = "guest"
INDEX_PAGE = ROOT / "server" / "static" / "index.html"
MAX_OPEN_JOBS_PER_USER = 3
MAX_QUEUED_JOBS = 20
MAX_STATEMENT_CHARS = 20_000
MAX_PROOF_CHARS = 50_000
MAX_LIBRARY_CHARS = 2_000_000
# Libraries a run may build on (orchestrator/library.py); uploads are kept
# per run under uploads/.
LIBRARY_DIR = ROOT / "libraries"
LIBRARY_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.lean$")
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


class Selection(BaseModel):
    provider: str
    model: str
    effort: str = ""


class Submission(BaseModel):
    uuid: str = ""
    statement: str = ""
    proof: str = ""
    orchestrator: Selection | None = None
    auditor: Selection | None = None
    reviewer: Selection | None = None
    writer: Selection | None = None
    subagent: Selection | None = None
    library: str = ""          # a file in libraries/
    library_text: str = ""     # or an uploaded library
    extend_library: bool = False
    after: str = ""            # a run that must end first


@app.get("/api/models")
def model_catalog(user: str = Depends(current_user)) -> dict:
    return models.catalog()


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

    try:
        selection = models.validate({role: getattr(body, role).model_dump()
                                     for role in models.ROLES if getattr(body, role)})
    except ValueError as error:
        raise HTTPException(400, str(error))

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

    options = library_options(body, run_id)
    store.add({"run_id": run_id, "owner": user, "uuid": uuid, "title": title,
               "dataset": str(dataset), "state": "awaiting_approval",
               "submitted": time.time(), "agents": json.dumps(selection),
               "options": json.dumps(options) if options else None})
    return {"run_id": run_id, "state": "awaiting_approval"}


def library_options(body: Submission, run_id: str) -> dict:
    """Run options for a submission's library and dependency."""
    options = {}
    if body.library and body.library_text:
        raise HTTPException(400, "give a library name or a library file, not both")
    if body.library:
        if not LIBRARY_NAME.match(body.library) or not (LIBRARY_DIR / body.library).exists():
            raise HTTPException(400, f"unknown library {body.library}")
        options["library"] = f"libraries/{body.library}"
    elif body.library_text.strip():
        if len(body.library_text) > MAX_LIBRARY_CHARS:
            raise HTTPException(413, "library file too long")
        path = LIBRARY_DIR / "uploads" / f"{run_id}.lean"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.library_text)
        options["library"] = str(path.relative_to(ROOT))
    if body.extend_library:
        if "library" not in options:
            raise HTTPException(400, "extend_library requires a library")
        options["extend_library"] = True
    if body.after:
        if store.get(body.after) is None:
            raise HTTPException(400, f"unknown run {body.after}")
        options["after"] = body.after
    return options


@app.get("/api/libraries")
def list_libraries(user: str = Depends(current_user)) -> list[dict]:
    """Libraries in libraries/: name, size, and number of declarations."""
    return [{"name": path.name, "chars": len(text := path.read_text()),
             "declarations": len(libraries.declared_names(text))}
            for path in sorted(LIBRARY_DIR.glob("*.lean"))]


def describe(job: dict) -> dict:
    result_dir = ROOT / "results" / job["run_id"]
    files = [name for name in RESULT_PAGES if (result_dir / name).exists()]
    if any(result_dir.glob("*_standalone.lean")):
        files.append("standalone.lean")
    files += [f"report{suffix}" for suffix in (".pdf", ".tex")
              if any((result_dir / "report").glob(f"*{suffix}"))]
    return {key: job[key] for key in ("run_id", "owner", "uuid", "title", "state", "status",
                                      "submitted", "started", "ended", "approved_by",
                                      "resumptions")} | {
        "position": store.queue_position(job["run_id"]), "files": files,
        "agents": job_selection(job)}


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
    elif name in ("report.pdf", "report.tex"):
        matches = sorted((result_dir / "report").glob(f"*{Path(name).suffix}"))
        path = matches[0] if matches else None
    else:
        path = result_dir / name if name in RESULT_PAGES else None
    if path is None or not path.exists():
        raise HTTPException(404, "no such file")
    media_type = "text/plain; charset=utf-8" if path.suffix in (".lean", ".tex") else None
    return FileResponse(path, media_type=media_type)


# Export layout: the files a reader opens first at the root of the folder,
# everything else in subfolders by purpose.
EXPORT_FOLDERS = {
    "lean_modules": "Lean modules of the solution (Definitions, Theorems, Solutions, Check) and their build log",
    "report_build": "LaTeX by-products of the report",
    "run_data": "Problem, summary and full trace of the run (JSON)",
    "logs": "Model calls and the orchestrator log",
}
STATUS_TEXT = {"proved": "proved; the Lean proof compiles and is verified",
               "lemmas_unproved": "not proved; some lemmas have no Lean proof",
               "failed": "not proved; the run stopped before a proof"}
# Pipeline role names as the page names them.
ROLE_NAMES = {pipeline: role for role, pipeline in models.ROLES.items()}


def model_label(spec: dict, version: str) -> str:
    """Readable model of an agent specification from a trace."""
    if spec.get("backend") in ("aiprover", "openai_compatible") and spec.get("model") == "aiprover":
        return models.AIPROVER_VERSIONS.get(version, "AIProver")
    info = models.CLAUDE_MODELS.get(spec.get("model"))
    label = info["label"] if info else spec.get("model", "?")
    return f"{label} ({spec['effort']} reasoning)" if spec.get("effort") else label


def export_path(relative: Path) -> str | None:
    """Where a file of results/<run_id>/ goes in the export; None to leave it out."""
    name, suffix = relative.name, relative.suffix
    if relative.parts[0] == "report":
        return name if suffix in (".tex", ".pdf") else f"report_build/{name}"
    if name.endswith("_standalone.lean") or suffix == ".html":
        return name
    if suffix == ".lean" or name == "module_build.log":
        return f"lean_modules/{name}"
    return f"run_data/{relative}"


def export_readme(record: dict, trace_agents: dict, files: list[str]) -> str:
    """A short guide to the exported folder; the models are those the trace
    records."""
    root_files = [name for name in files if "/" not in name]
    folders = [folder for folder in EXPORT_FOLDERS if any(name.startswith(folder + "/") for name in files)]
    guide = {
        "_standalone.lean": "complete Lean 4 formalization and proof in one file (Mathlib)",
        ".pdf": "report: the formalized statement and proof in mathematical language, with the Lean code",
        ".tex": "LaTeX source of the report",
        "trace.html": "walkthrough of the run, step by step (open in a browser)",
        "trace_replay.html": "animated replay of the run (open in a browser)",
        "trace_graph.html": "graph of the run (open in a browser)",
    }
    lines = [f"# {record['title']}", "",
             f"Problem `{record['uuid']}`, run `{record['run_id']}`.",
             f"Result: {STATUS_TEXT.get(record['status'], record['status'] or record['state'])}.", "",
             "## Contents", ""]
    for name in root_files:
        description = next((text for key, text in guide.items() if name.endswith(key)), "")
        lines.append(f"- `{name}`" + (f": {description}" if description else ""))
    lines += [f"- `{folder}/`: {EXPORT_FOLDERS[folder]}" for folder in folders]
    version = next((choice["model"] for choice in record["agents"].values()
                    if choice["provider"] == "aiprover"), "trained")
    roles = sorted(trace_agents, key=lambda name: list(ROLE_NAMES).index(name)
                   if name in ROLE_NAMES else len(ROLE_NAMES))
    if roles:
        lines += ["", "## Models", ""]
        lines += [f"- {ROLE_NAMES.get(role, role)}: {model_label(trace_agents[role], version)}"
                  for role in roles]
    if any(name.endswith("_standalone.lean") for name in root_files):
        lines += ["", "The standalone file compiles with Lean v4.23.0 and Mathlib v4.23.0 in a Lake project:",
                  "`lake env lean <file>`."]
    return "\n".join(lines) + "\n"


@app.get("/api/runs/{run_id}/export")
def export_run(job: dict = Depends(owned_job)) -> FileResponse:
    """The run as a zip of one folder: the standalone Lean file, the report
    (.tex, .pdf), the trace pages and a README at its root; Lean modules, LaTeX
    by-products, run data and logs in the subfolders of EXPORT_FOLDERS."""
    run_id = job["run_id"]
    rows = (json.loads(line) for line in open(job["dataset"]) if line.strip()) \
        if Path(job["dataset"]).exists() else ()
    problem = next((row for row in rows if row["uuid"] == job["uuid"]), None)
    record = describe(job) | {"problem": {key: value for key, value in (problem or {}).items()
                                          if not key.startswith("_")}}
    entries = {"run_data/problem.json": json.dumps(record, indent=1, ensure_ascii=False)}
    sources = {}
    result_dir, log_dir = ROOT / "results" / run_id, ROOT / "logs" / run_id
    for path in sorted(result_dir.rglob("*")) if result_dir.is_dir() else []:
        target = export_path(path.relative_to(result_dir)) if path.is_file() else None
        if target:
            sources[target] = path
    for path in sorted(log_dir.rglob("*")) if log_dir.is_dir() else []:
        if path.is_file():
            sources[f"logs/{path.relative_to(log_dir)}"] = path
    # Root files first (Lean, PDF, TeX, pages), then the subfolders in order.
    order = lambda name: ("/" in name, list(EXPORT_FOLDERS).index(name.split("/")[0]) if "/" in name else 0,
                          not name.endswith(".lean"), not name.endswith(".pdf"),
                          not name.endswith(".tex"), name)
    sources = dict(sorted(sources.items(), key=lambda item: order(item[0])))
    trace_path = result_dir / "trace.json"
    try:
        trace_agents = json.loads(trace_path.read_text()).get("agents") or {}
    except (OSError, json.JSONDecodeError):
        trace_agents = {}
    entries = {"README.md": export_readme(record, trace_agents, list(entries) + list(sources)),
               **entries}
    archive = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for target, text in entries.items():
            bundle.writestr(f"{run_id}/{target}", text)
        for target, path in sources.items():
            bundle.write(path, f"{run_id}/{target}")
    archive.close()
    return FileResponse(archive.name, media_type="application/zip", filename=f"{run_id}.zip",
                        background=BackgroundTask(os.unlink, archive.name))


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
