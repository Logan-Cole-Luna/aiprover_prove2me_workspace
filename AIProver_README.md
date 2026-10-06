# Orchestration

A multi-model Lean 4 formalization and proving pipeline. An orchestrator
model (captain) formalizes an informal result, commissions a blind audit of
the formalization, decomposes the proof into lemmas, and assembles the final
solution. Worker models (solvers) prove the lemmas in parallel under Lean
error feedback. An independent reviewer model referees the verified solution
against the source, and a LaTeX report with a PDF presents each statement
and proof in mathematical notation beside its Lean code. The roles and the Theorems/Definitions/Solutions layout follow
the [prove2me](https://github.com/prove2me/prove2me_workspace) playbooks
(`prove2me_workspace/references/mission_{captain,auditor,solver}.md`).
Verification is local; the prove2me server is not contacted.

Each role is served by a pluggable agent (`orchestrator/agents.py`), chosen
per role in the config file:

| Backend | Serves | Use |
|---|---|---|
| `claude_cli` | Claude models through the account session (`claude -p`, tools disabled) | captain |
| `openai_compatible` | Any OpenAI-schema chat endpoint: local vLLM, hosted open weights, proprietary gateways | auditor, solvers |
| `aiprover` | The AIProver harness (`AIProver/AIProver_plugin`, submodule): one agentic Lean session per sample, lean-lsp tools, model from `aiprover/aiprover*.toml` | solvers |
| `python` | A user subclass of `agents.Agent`, loaded by import path | custom agents |

The backends follow `../MixtureOfMathExperts/scripts/utils/generators.py`
(`claude_cli` and `vllm_endpoint` teachers). The default configuration runs
the captain on `claude-sonnet-5` and the auditor and solvers on
`Qwen/Qwen3-4B-Instruct-2507`, served locally by vLLM on one RTX 5070 Ti
(16 GB). `orchestrator/config_claude.json` runs all roles on Claude models;
`orchestrator/config_aiprover_vista.json` runs the captain on Claude and the
solvers on the AIProver model served on TACC Vista (see Deployment).

```mermaid
flowchart LR
    P[Informal statement + proof<br/>JiatuBook dataset] --> F[Captain: formalize<br/>Definitions + Theorem]
    F -->|Lean errors| F
    F --> A[Auditor agent:<br/>blind read-back]
    A --> J{Captain: faithful?}
    J -->|REVISE| F
    J -->|FAITHFUL| S[Captain: sketch<br/>lemmas as sorry + main proof]
    S -->|Lean errors| S
    S --> W[Solver agents ×N per lemma<br/>parallel, Lean feedback]
    W -->|lemma failed| S
    W --> V[Assemble + verify<br/>no sorry, standard axioms,<br/>type matches Theorem]
    V --> RV{Reviewer:<br/>faithful to source?}
    RV --> L[Writer: LaTeX report<br/>Lean code inserted verbatim;<br/>reviewer checks statements]
    L --> R[results/&lt;run_id&gt;<br/>Lean, trace, PDF]
    subgraph Backends
        C1[claude_cli] ~~~ C2[openai_compatible<br/>local vLLM] ~~~ C3[python<br/>custom Agent]
    end
```

## Layout

| Path | Content |
|---|---|
| `orchestrator/agents.py` | Agent interface, backends, `AgentPool` (routing by role, retries, call records) |
| `orchestrator/aiprover_agent.py` | AIProver solver backend: job submission, sample collection, proof extraction |
| `orchestrator/claude_cli.py` | `claude -p` invocation, usage-limit parsing, account check |
| `orchestrator/lean_check.py` | `lake env lean` checks, forbidden-construct filter, axiom parsing |
| `orchestrator/prompts.py` | Captain, auditor and solver prompts |
| `orchestrator/pipeline.py` | Phases: formalize → audit → sketch → prove → assemble → review → report |
| `orchestrator/library.py` | Libraries of verified results that runs build on (`python3 -m orchestrator.library add <library> results/<run_id>` appends a verified run) |
| `libraries/` | Libraries; `gsimple.lean`: the presentation of $G(\mathfrak m)$ and the verified results of the G-Simple draft |
| `orchestrator/report.py` | LaTeX report and PDF of a verified solution (`python3 -m orchestrator.report <dir>` recompiles a saved report) |
| `orchestrator/trace.py` | Step-by-step run record (`trace.json`), continued in place on resume |
| `orchestrator/resume.py` | Rebuilds a run's state from its trace for `--resume` |
| `orchestrator/structures.py` | Run data structures and parsing of model-written Lean |
| `orchestrator/trace_view.py` | Renders `trace.json` as a chapter walkthrough (`trace.html`, template `trace_view.html`) and an animated replay (`trace_replay.html`), whose step drawer holds each AIProver sample's final file, Lean results, session transcript and reasoning; shared code in `trace_common.{css,js}` and the style in `trace_theme.css` |
| `orchestrator/run.py`, `config*.json` | Entry point; agent per role and budgets (`config.json` Sonnet + local chat solvers, `config_claude.json` all Claude, `config_aiprover.json` Haiku + local AIProver solvers, `config_aiprover_vista.json` Opus captain + AIProver model on Vista) |
| `aiprover/aiprover.toml`, `aiprover_vista.toml` | AIProver configuration: endpoint (local vLLM, or the Vista server through an SSH tunnel and handoff file); venvs, ripgrep and job directory under `aiprover/` |
| `AIProver/` | AIProver submodule (`PrithwishJana/AIProver`): plugin, CLI, pinned harness |
| `scripts/serve_aiprover_vista.sbatch`, `submit_aiprover_vista.sh` | AIProver model server on Vista GH200: Ray across nodes, vLLM tensor parallel, handoff file |
| `scripts/unpack_experts_vista.sbatch` | Fused-expert checkpoint to per-expert tensors for vLLM (CPU `gg` node) |
| `data/val_JiatuBook_unlabelled.jsonl` | JiatuBook validation problems (default `--dataset`) |
| `data/gsimple.jsonl` | Lemma 3.1, Corollary 3.2 and Theorem 3.3 of the G-Simple draft (`example/GSimple.tex`), with the presentation of $G(\mathfrak m)$ as setting |
| `server/` | Query server: HTTP submission of problems, run queue, progress stream, results; reasoning proxy for AIProver (`aiprover_tacc.md` §5) |
| `scripts/serve_local.sh` | vLLM OpenAI-compatible server for a local model (`.venv_serve`, vLLM 0.30.0) |
| `prove2me_workspace/` | Lean project (Lean v4.23.0, Mathlib `37df177`, clone of `prove2me/prove2me_workspace`); `.lake/packages` links the AIProver Lean project; final modules are written to `Definitions/`, `Theorems/`, `Solutions/` |
| `results/<run_id>/` | `trace.json` (every step, for replay), `trace.html` (walkthrough), `trace_replay.html` (animated replay with per-sample Lean results, sessions and reasoning), `summary.json`, Lean modules, standalone file, JiatuBook-format output row, `report/` (LaTeX source and PDF) |
| `logs/<run_id>/` | `calls.jsonl` (every prompt and reply), `run.log` |
| `temp/<run_id>/` | Live progress log and every Lean attempt file |

## Usage

```bash
claude auth status                         # captain: account session
scripts/serve_local.sh &                   # auditor/solvers: local vLLM on :8000
PYTHONPATH=. python3 -m orchestrator.agents          # probe every role
PYTHONPATH=. python3 -m orchestrator.run \
    --problem-uuid JiatuBook_BoundedArithmetic_000004 \
    --config orchestrator/config.json --workers 4
```

Runs are resumable. Every completed step (compiled formalization, audit
verdict, accepted sketch, proved lemma, replan) is recorded in
`results/<run_id>/trace.json`; `--resume --run-id <run_id>` replays these
records (`orchestrator/resume.py`) and continues from the last completed step
with the current configuration. A run that stops on an infrastructure failure
(a model call failing after all retries) resumes automatically up to
`--max-restarts` times (default 2).

A custom subagent replaces a role by pointing its entry at an
OpenAI-compatible endpoint (`base_url`, `model`, optional `api_key_env`) or at
a Python class:
`{"backend": "python", "class": "my_agent.module:MyAgent", "model": "..."}`,
where `MyAgent` subclasses `orchestrator.agents.Agent` and implements
`complete_once(prompt, system_prompt) -> Completion`.

A solution is accepted when the standalone file compiles without `sorry`,
`#print axioms solution` lists only `propext`, `Classical.choice` and
`Quot.sound`, and a check module importing both `Theorems.Thm_<slug>` and
`Solutions.Sol_<slug>` elaborates `example : type_of% @<slug> := @solution`.

An accepted solution is then refereed by the `reviewer` role (default: a
fresh agent of the captain's model; served: `claude-opus-5-5`, high effort),
which sees only the source and the verified file and reports on the
definitions, the statement, the proof's correspondence with the source proof,
and concerns for a reader. The run's status is `proved` only with verdict
FAITHFUL; otherwise `review_flagged`. The `writer` role then writes the
report in `results/<run_id>/report/`: statement, formalization, and every
lemma in mathematical notation with its proof, each followed by its Lean
declaration as checked (inserted by the generator, never retyped by a model)
and the agent that proved it; the referee's findings; the complete Lean file
as appendix. The reviewer checks every statement of the text against its
Lean code and the writer corrects the discrepancies once. The PDF is
compiled with pdfLaTeX without shell escape. `final_claude_calls` (default 8)
are added to `max_claude_calls` for these two stages.

A run may build on a library of verified results (`--library <file>`, or a
`library` field in the problem's row): a Lean file of definitions and proved
theorems that compiles on its own. The captain then formalizes only the new
statement against the library's definitions, adding definitions only where
the library lacks them; the library is the prefix of every checked file, so
the final solution still compiles standalone. Solvers and the captain may cite
the library's theorems, and the report cites them instead of presenting them.
With `--extend-library true`, a proved and reviewed run appends its result
to the library: its new definitions, its lemmas, and its main proof under the
target theorem's name. The extended library must compile without `sorry`.

A run is rendered as three linked, self-contained HTML pages: a step-by-step
walkthrough (problem, algorithm, prompts, audit, solver timeline,
verification) and a pannable graph of the captain, its subtasks, the agents
assigned to each, and their attempts; and a replay that animates the run
top to bottom on its recorded clock, from the input problem through the
parallel solver lanes to the verified proof, with a JPEG export of the
completed tree. Every step opens its full record
(prompt, reply, Lean source, compiler output). Generate both with
`python3 -m orchestrator.trace_view results/<run_id>/trace.json`.

`aiprover_lemma_concurrency` limits the AIProver lemma jobs in flight, since
concurrent jobs share one model server.
`max_claude_calls` in a config (or `--max-claude-calls`) bounds the
`claude_cli` calls of a run; at the bound the run stops with
`error_kind = budget`. Problems can also be submitted over HTTP through
the query server (`server/app.py`, `aiprover_tacc.md` §5).

The exported row can be scored with
`../PartitionAndProve/llm_inferAndEval/evaluate.py` for comparison with the
JiatuBook benchmark systems.

## Deployment: AIProver on Vista

The captain (Claude, through `claude -p`), the orchestrator, the AIProver
harness, Lean and the lean-lsp tools run on the orchestration VM
(`129.114.35.157`); only the model runs on Vista, whose compute nodes have
no internet egress (TACC.md §4). Vista has one GH200 per node, so tensor
parallelism spans nodes through a Ray cluster.

```mermaid
flowchart LR
    subgraph VM[Orchestration VM]
        O[orchestrator.run] --> C[captain: claude -p]
        O --> H[AIProver harness<br/>vibe + lean-lsp + Lean 4.23]
    end
    subgraph Vista[TACC Vista]
        L[login node] -->|handoff file| N[gh nodes x2<br/>Ray + vLLM PP=2]
    end
    H -->|ssh -L via vista.sock| L
```

Environment (one-time, on the VM): Lean project
`~/workspace/lean_projects/TmpProjDir` (Lean v4.23.0, Mathlib `37df177`,
REPL, cslib), venvs and ripgrep under `aiprover/`, provisioned by
`AIPROVER_CONFIG=aiprover/aiprover.toml AIProver/AIProver_plugin/setup.sh deps`.

```bash
# Vista login node, repository root: start the model server
scripts/submit_aiprover_vista.sh /work/.../aiprover_ckpt            # gh, 2 nodes, 48 h
scripts/submit_aiprover_vista.sh /work/.../aiprover_ckpt gh-dev 2   # launch check, 2 h

# VM: ControlMaster (password + MFA once), then verify and run
ssh -fNM -S ~/.ssh/vista.sock -o ControlPersist=12h -o ServerAliveInterval=60 \
    loganluna@vista.tacc.utexas.edu
AIPROVER_CONFIG=aiprover/aiprover_vista.toml AIProver/AIProver_plugin/bin/aiprover doctor
PYTHONPATH=. python3 -m orchestrator.run \
    --problem-uuid JiatuBook_BoundedArithmetic_000004 \
    --config orchestrator/config_aiprover_vista.json --run-id vista_000004
```

The server job writes `node=<n> port=<p> job=<id>` to
`$SCRATCH/servers/aiprover_server.txt` before the weights load; the AIProver
CLI reads it through the ControlMaster on every tunnel (re)open, so a
resubmitted server is followed without configuration changes. After a
resubmission, `aiprover tunnel down` followed by `aiprover tunnel up`
replaces a forward that still targets the previous node. The trained model
(`/work/11428/pjana/aiprover_model`, Mistral format, FP8, 112 GB) is served as
is, pipeline parallel over two nodes. Fused-expert HF
checkpoints are first converted to per-expert tensors
(`scripts/unpack_experts_vista.sbatch`), the layout vLLM's loader reads. A bf16
checkpoint of ~222 GiB exceeds the HBM of two nodes (2 x 95 GiB); at two
nodes the job offloads the excess weights per rank to Grace memory
(`--cpu-offload-gb`, computed from the checkpoint size), or quantizes online
with `QUANTIZATION=fp8`. Four nodes (TP=4) serve bf16 entirely from HBM.

## Results

Problem `JiatuBook_BoundedArithmetic_000004` (`prop: bounding ite base`,
PV ⊢ ITR(ε, z∘ε) = ε), formalize-and-prove from the informal statement and
proof. Captain `claude-sonnet-5`; 4 solver chains per lemma, 4 repair rounds,
up to 2 replans.

| Run | Auditor / solvers | Status | Audit | Lemmas proved | Calls (captain / auditor / solver) | Wall time |
|---|---|---|---|---|---|---|
| `smoke_000004` | `claude-haiku-4-5` | proved, verified | FAITHFUL | 3 / 3 | 3 / 1 / 19 | 13.1 min |
| `local_000004` | `Qwen3-4B-Instruct-2507` (local vLLM) | lemmas unproved | FAITHFUL | 0 / 1 (3 sketches) | 9 / 1 / 60 | 16.1 min |
| `vista_smoke_000004` | AIProver base model (Vista, 2 x GH200), Sonnet 5.5 captain and auditor | proved, verified | FAITHFUL | 1 / 1 | 11 / 1 / 1 | 36.0 min |
| `srv_20261003_232555_000004` | AIProver trained model (FP8, Vista, 2 x GH200), Sonnet 5.5 captain and auditor | proved, verified | FAITHFUL | 1 / 1 | 3 / 1 / 1 | 27.1 min |

In `smoke_000004` the formalization encodes PV at the object level (term
syntax, an inductive derivability relation with rules L0 to L4, the defining
axioms and structural induction); the solution uses only the `propext`
axiom. In `local_000004` the formalization and audit completed, and the
local solvers produced no compiling proof of the structural-induction lemma:
of 45 Lean checks, the dominant errors were unresolved constructor names
(for example `ax_ITR_eps` for `Deriv.ax_ITR_eps`) and failed `apply`
unifications. Per-step records are in `results/<run_id>/trace.json`, rendered
as `trace.html` and `trace_replay.html`.

G-Simple draft (Carbone et al., `example/GSimple.tex`), presentation of
$G(\mathfrak m)$; served runs, AIProver trained model (FP8, Vista,
2 x GH200) as solver, Sonnet 5.5 auditor. Theorem 3.3 has no prior Lean
formalization.

| Run | Statement | Captain | Status | Lemmas proved | Calls (captain / auditor / solver) | Wall time |
|---|---|---|---|---|---|---|
| `srv_20261004_123233_GSimple_Lemma_3_1` | Lemma 3.1 | Sonnet 5.5 | proved, verified | 5 / 5 | 5 / 1 / 7 | 93.0 min |
| `srv_20261004_123234_GSimple_Corollary_3_2` | Corollary 3.2 | Sonnet 5.5 | proved, verified | 3 / 3 | 5 / 1 / 5 | 49.3 min |
| `srv_20261004_123235_GSimple_Theorem_3_3` | Theorem 3.3 ($G(\mathfrak m)$ perfect) | Sonnet 5.5, then Opus 5.5 | proved, verified | 26 / 26 (2 sketches) | 7 / 1 / 45 | 15.3 h |

In Theorem 3.3, 23 lemmas were proved by AIProver within the first
attempts; of the remaining three, `h2_mem` and `h1_cube_eq` were proved by
AIProver on their third attempt (8 sessions for the last lemma), and
`yy_mem` by the captain on hand-back after four failed AIProver attempts.
The solution uses only `propext`, `Classical.choice` and `Quot.sound`.
