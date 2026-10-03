# Orchestration

A multi-model Lean 4 formalization and proving pipeline. An orchestrator
model (captain) formalizes an informal result, commissions a blind audit of
the formalization, decomposes the proof into lemmas, and assembles the final
solution. Worker models (solvers) prove the lemmas in parallel under Lean
error feedback. The roles and the Theorems/Definitions/Solutions layout follow
the [prove2me](https://github.com/prove2me/prove2me_workspace) playbooks
(`prove2me_workspace/references/mission_{captain,auditor,solver}.md`).
Verification is local; the prove2me server is not contacted.

Each role is served by a pluggable agent (`orchestrator/agents.py`), chosen
per role in the config file:

| Backend | Serves | Use |
|---|---|---|
| `claude_cli` | Claude models through the account session (`claude -p`, tools disabled) | captain |
| `openai_compatible` | Any OpenAI-schema chat endpoint: local vLLM, hosted open weights, proprietary gateways | auditor, solvers |
| `aiprover` | The AIProver harness (`../PartitionAndProve/AIProver_plugin`): one agentic Lean session per sample, lean-lsp tools, model from `aiprover/aiprover.toml` | solvers |
| `python` | A user subclass of `agents.Agent`, loaded by import path | custom agents |

The backends follow `../MixtureOfMathExperts/scripts/utils/generators.py`
(`claude_cli` and `vllm_endpoint` teachers). The default configuration runs
the captain on `claude-sonnet-5` and the auditor and solvers on
`Qwen/Qwen3-4B-Instruct-2507`, served locally by vLLM on one RTX 5070 Ti
(16 GB). `orchestrator/config_claude.json` runs all roles on Claude models.

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
    V --> R[results/&lt;run_id&gt;]
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
| `orchestrator/pipeline.py` | Phases: formalize → audit → sketch → prove → assemble |
| `orchestrator/trace.py` | Step-by-step run record (`trace.json`), continued in place on resume |
| `orchestrator/resume.py` | Rebuilds a run's state from its trace for `--resume` |
| `orchestrator/structures.py` | Run data structures and parsing of model-written Lean |
| `orchestrator/trace_view.py` | Renders `trace.json` as a chapter walkthrough (`trace.html`, template `trace_view.html`) an orchestration graph (`trace_graph.html`) and an animated replay (`trace_replay.html`); shared code in `trace_common.{css,js}` |
| `orchestrator/run.py`, `config*.json` | Entry point; agent per role and budgets (`config.json` Sonnet + local chat solvers, `config_claude.json` all Claude, `config_aiprover.json` Haiku + local AIProver solvers) |
| `aiprover/aiprover.toml` | AIProver configuration (endpoint = local vLLM; venvs, ripgrep and job directory under `aiprover/`) |
| `scripts/serve_local.sh` | vLLM OpenAI-compatible server for a local model (`.venv_serve`, vLLM 0.30.0) |
| `prove2me_workspace/` | Lean project (Lean v4.23.0, Mathlib `37df177`); final modules are written to `Definitions/`, `Theorems/`, `Solutions/` |
| `results/<run_id>/` | `trace.json` (every step, for replay), `trace.html` (walkthrough), `trace_graph.html` (graph), `trace_replay.html` (animated replay), `summary.json`, Lean modules, standalone file, JiatuBook-format output row |
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
A run is rendered as three linked, self-contained HTML pages: a step-by-step
walkthrough (problem, algorithm, prompts, audit, solver timeline,
verification) and a pannable graph of the captain, its subtasks, the agents
assigned to each, and their attempts; and a replay that animates the run
top to bottom on its recorded clock, from the input problem through the
parallel solver lanes to the verified proof, with a JPEG export of the
completed tree. Every step opens its full record
(prompt, reply, Lean source, compiler output). Generate both with
`python3 -m orchestrator.trace_view results/<run_id>/trace.json`.

The exported row can be scored with
`../PartitionAndProve/llm_inferAndEval/evaluate.py` for comparison with the
JiatuBook benchmark systems.

## Results

Problem `JiatuBook_BoundedArithmetic_000004` (`prop: bounding ite base`,
PV ⊢ ITR(ε, z∘ε) = ε), formalize-and-prove from the informal statement and
proof. Captain `claude-sonnet-5`; 4 solver chains per lemma, 4 repair rounds,
up to 2 replans.

| Run | Auditor / solvers | Status | Audit | Lemmas proved | Calls (captain / auditor / solver) | Wall time |
|---|---|---|---|---|---|---|
| `smoke_000004` | `claude-haiku-4-5` | proved, verified | FAITHFUL | 3 / 3 | 3 / 1 / 19 | 13.1 min |
| `local_000004` | `Qwen3-4B-Instruct-2507` (local vLLM) | lemmas unproved | FAITHFUL | 0 / 1 (3 sketches) | 9 / 1 / 60 | 16.1 min |

In `smoke_000004` the formalization encodes PV at the object level (term
syntax, an inductive derivability relation with rules L0 to L4, the defining
axioms and structural induction); the solution uses only the `propext`
axiom. In `local_000004` the formalization and audit completed, and the
local solvers produced no compiling proof of the structural-induction lemma:
of 45 Lean checks, the dominant errors were unresolved constructor names
(for example `ax_ITR_eps` for `Deriv.ax_ITR_eps`) and failed `apply`
unifications. Per-step records are in `results/<run_id>/trace.json`, rendered
as `trace.html`, `trace_graph.html` and `trace_replay.html`.
