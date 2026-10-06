# Query server

A web service on the orchestration VM through which problems are submitted to
the formalize-and-prove pipeline (`aiprover_tacc.md`). Submissions are
approved by a token holder, run with
`orchestrator/config_aiprover_vista_served.json` (Opus 5.5 captain, Sonnet 5.5
auditor, at most 10 Claude calls per run), and the AIProver model server on Vista is started and released
on demand. Details: `aiprover_tacc.md` §5.

## Prerequisites (VM)

- ControlMaster to Vista, reopened with MFA every 12 h:
  ```bash
  ssh -fNM -S ~/.ssh/vista.sock -o ControlPersist=12h -o ServerAliveInterval=60 \
      loganluna@vista.tacc.utexas.edu
  ```
- Approver tokens in `server/tokens.json` (`{"<token>": "<name>"}`, see
  `server/tokens.example.json`); a token is generated with
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. The file is
  re-read on change.

## Service

```bash
mkdir -p ~/.config/systemd/user
cp scripts/aiprover_query_server.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aiprover_query_server
sudo loginctl enable-linger loganluna          # keep running after logout
cp scripts/aiprover_reasoning_proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aiprover_reasoning_proxy   # records AIProver's reasoning
```

| Operation | Command |
|---|---|
| Status | `systemctl --user status aiprover_query_server` |
| Restart (after code or unit changes) | `systemctl --user restart aiprover_query_server` |
| Stop | `systemctl --user stop aiprover_query_server` |
| Health | `curl http://127.0.0.1:8443/api/health` |

Restarting does not interrupt a run: the run continues in its own session and
is adopted by the new server process.

Settings in the unit (`Environment=`, then `daemon-reload` and restart):

| Variable | Default | Effect |
|---|---|---|
| `QUERY_SERVER_AUTH` | `on` (unit sets `off`) | `off`: no token needed to submit or view; approval still needs one |
| `QUERY_SERVER_MAX_RUNS` | `2` | Runs in progress at once (same problem never twice) |
| `VISTA_PARTITION`, `VISTA_NODES`, `VISTA_WALL_TIME` | `gh-dev`, `2`, `02:00:00` (unit sets `gh`, `12:00:00`) | Model server job submitted on demand |
| `VISTA_CHECKPOINT` | `/work/11428/pjana/aiprover_model` | Checkpoint served (trained AIProver model, Mistral format, FP8) |

The port is set by `--port` in `ExecStart`.

Each run carries a model choice for each role: orchestrator (captain),
auditor, reviewer, writer and subagent (solver), selected on the page and stored in the `agents` column of
`jobs.db` (`server/models.py`; `GET /api/models` lists the choices). A role
uses Claude (Opus 5.5, Sonnet 5.5 or Haiku 4.5, with a reasoning level where
the model has one) or an AIProver version (`trained`, `base`; no reasoning
level). An AIProver chat role (all but the subagent) is called through the reasoning
proxy as an OpenAI-compatible endpoint. All roles use the same AIProver version, since the
model server serves one checkpoint (`VISTA_CHECKPOINT`, `VISTA_CHECKPOINT_BASE`).
The worker starts runs that share the served checkpoint together, replaces the
model server job when the next run needs the other version, and starts runs
that use no AIProver without a model server. With a Claude subagent the run's Claude call limit is 40
instead of 10.

A run can carry settings over the served config in the `options` column of
`server/state/jobs.db` (JSON, e.g. `{"aiprover_attempts_per_lemma": 5}`),
passed to `orchestrator.run` as command-line options.

## Access

- Direct: `http://129.114.35.157:8443/`, once the port is open in the VM's
  security group.
- SSH tunnel: `ssh -N -L 8443:localhost:8443 loganluna@129.114.35.157`, then
  `http://localhost:8443/`.

On the page, users submit a JiatuBook UUID or an informal statement and proof,
follow the progress log, and open the trace pages and the standalone Lean
file. **Approver sign-in** (token) shows Approve and Reject on waiting runs.

**Libraries.** A run may build on a library of verified results
(`orchestrator/library.py`): a file from `libraries/` (listed by
`GET /api/libraries`) or an uploaded Lean file, which must compile on its own
without `sorry`. The run uses the library's definitions and may cite its
theorems. With "Add the result to the library", a run that is proved and
reviewed appends its result (definitions, lemmas, and the theorem under its
own name) to the library, kept only if the library still compiles.
`POST /api/runs` takes `library` (a name in `libraries/`) or `library_text`
(the file), `extend_library`, and `after` (a run that must end before this
one starts, for chains of results that build on each other).

The download icon of a run (`GET /api/runs/<run_id>/export`) returns
`<run_id>.zip`, one folder: the standalone Lean file, the report (`.tex`,
`.pdf`), the trace pages and a `README.md` at its root; `lean_modules/`,
`report_build/`, `run_data/` (problem, summary, trace) and `logs/` below it.

## State

| Path | Content |
|---|---|
| `server/state/jobs.db` | Runs and the Vista jobs the server submitted |
| `server/state/problems/` | Free-text problems as one-row datasets |
| `libraries/`, `libraries/uploads/` | Libraries of verified results; uploaded libraries per run |
| `results/<run_id>/`, `logs/<run_id>/`, `temp/<run_id>/` | Run outputs, as for manual runs |
| `temp/<run_id>.out` | Orchestrator output of a served run |



To run webpage, run in local device terminal:
```bash
ssh -N -L 8443:localhost:8443 loganluna@129.114.35.157
```