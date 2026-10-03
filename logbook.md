# Logbook

## 2026-09-29: Sonnet→Haiku smoke test on JiatuBook 000004

- Environment: `prove2me_workspace` pinned to Lean v4.23.0 / Mathlib `37df177`
  (the JiatuBook evaluation environment); models called through the Claude
  account via `claude -p`.
- Target: `JiatuBook_BoundedArithmetic_000004` (`prop: bounding ite base`),
  solved in the benchmark by aristotle, AIProver, Numina and Opus-5.
- Run `smoke_000004`: formalization compiled at the first attempt and was
  judged FAITHFUL from the Haiku read-back; the sketch had three lemmas
  (`concat_eps_subst`, `ITR_eps_var_eps`, `ITR_eps_subst`); all were proved
  by Haiku solvers (19 solver calls). Final solution verified: compiles, no
  `sorry`, axioms `[propext]`, type equal to the target theorem.
- Wall time 787 s; notional cost 0.60 USD (Sonnet) + 1.43 USD (Haiku).
- Full trace: `results/smoke_000004/trace.json`.

## 2026-09-29: Trace walkthrough page

- `orchestrator/trace_view.py` renders any `trace.json` into
  `results/<run_id>/trace.html`: ten chapters (overview, problem, algorithm
  flow chart, roles and prompts, formalization, blind audit, sketch, solver
  timeline, verification, filterable step log) and a drawer with the prompt,
  reply, Lean source and compiler errors of each step.
- `trace_graph.html`, rendered alongside: pan/zoom graph with the captain as
  root, subtasks (formalize, audit, verdict, sketch, one node per lemma,
  assemble), their agents, and attempt chains with repair edges and lemma
  dependencies; subtasks fold, nodes open the same step records.
- `trace_replay.html`: top-to-bottom animation on the trace clock (input →
  captain → formalize/audit/verdict/sketch → lemma fan-out → solver lanes by
  round → winning proofs → assembly → verified proof). Play/pause, 5–60×
  speed, scrubber, event stepping, narration, event log, auto-following
  camera; the full graph is shown faintly before playback.
- Replay "Export JPG": renders the completed tree (end-of-run state, header
  with run id, status, duration, models) at 2× to a JPEG; saved through the
  artifact `downloads` capability when published, a browser download when
  opened locally.

## 2026-09-30: Pluggable subagents; local open-weights solvers

- `orchestrator/agents.py`: `Agent` interface with backends `claude_cli`,
  `openai_compatible` and `python` (custom class by import path);
  `AgentPool` routes calls by role, retries transient failures and records
  each call in `calls.jsonl` and `trace.json` (fields `backend`, `model`,
  `usage`). Roles are configured in `config.json` (captain on Claude,
  auditor and solvers local) and `config_claude.json` (all Claude).
- Local serving: `.venv_serve` (uv, Python 3.12, vLLM 0.30.0, torch
  2.13.0+cu132), `scripts/serve_local.sh`; Qwen3-4B-Instruct-2507 in bf16,
  max length 24576, GPU memory utilization 0.84, KV cache 26.8k tokens.
- Run `local_000004` (captain Sonnet 5, auditor and solvers Qwen3-4B):
  formalization compiled at the first attempt, audit FAITHFUL, sketch with one
  lemma (`ITR_eps_var_eq_eps`, structural induction); 60 solver calls over
  three sketches produced no compiling proof. Status `lemmas_unproved`,
  963 s.

## 2026-10-01: Resumable runs

- `orchestrator/resume.py` rebuilds the state of a run from the decision
  steps of its trace (formalization, audit verdicts, sketches, proved lemmas,
  replans); `--resume --run-id <id>` continues from the last completed step.
  The trace is extended in place, its clock continues, and each resumption is
  listed under `resumptions`. Call and usage totals are re-read from
  `calls.jsonl`.
- Model calls that fail after all retries raise `AgentCallError`
  (`error_kind: infrastructure`); `run.py` resumes such runs automatically
  (`--max-restarts`, `--restart-delay`). SIGTERM unwinds the run so the
  outcome is recorded.
- Verified on copies of `smoke_000004` truncated before assembly (resumed with
  no model calls, verified) and with one lemma unproved (3 solver calls,
  verified).

## 2026-10-01: Unsolved problem 000020 (`thm: ind multiple var`)

- Target: two-variable structural induction in PV; no system in the
  JiatuBook evaluation produced a semantically correct proof (16 produced
  type-correct output).
- Run `local_000020` (captain Sonnet 5, auditor and solvers Qwen3-4B):
  formalization compiled at the first attempt and was judged FAITHFUL; it
  encodes PV functions as variable-free combinators (`PVFunc n` with `proj`,
  `comp`, `srec`) and omits the length bound of limited recursion. Sketch with
  three lemmas (`congr_app_two`, `itr_self_trim`, `key_diag`); no lemma proved
  in the first round. Cancelled during the first replan.
- Run `jiatu_000020_sonnet_haiku`: forked from `local_000020` at the prove
  stage (`--fork-from`), solvers Haiku 4.5. `congr_app_two` proved at the
  first attempt; `itr_self_trim` unproved by 3 of 4 chains when cancelled
  (outcome `interrupted`, resumable).
- `run.py --fork-from <run> --fork-stage <stage>` starts a new run from the
  steps of another run before the given stage.

## 2026-10-02: AIProver harness as solver backend

- AIProver (`../PartitionAndProve/AIProver_plugin`, champion harness
  `d01_r04`, mistral-vibe 2.24.2, lean-lsp-mcp 0.30.0) provisioned against
  `aiprover/aiprover.toml`: direct endpoint at the local vLLM server, venvs,
  ripgrep and job directory under `aiprover/`, Lean project v4.23.0. `doctor`:
  12/13 PASS; the reasoning check fails by design (non-reasoning model).
- `orchestrator/aiprover_agent.py`: `aiprover` backend; one job per lemma with
  `workers` samples, fixed context (definitions, earlier lemmas) and fixed
  statement; each sample's proof is extracted and passed through the pipeline's
  Lean gate. Configuration `config_aiprover.json` (Haiku captain, Qwen auditor,
  Qwen through AIProver).
- Serving for the harness: fp8 weights and KV cache, tool calling (`hermes`),
  28,672-token context at 0.565 GPU memory utilization alongside other GPU
  applications. vibe's compaction threshold is lowered to 18,000 tokens through
  `VIBE_MODELS` (environment layer above the harness's config file); the
  harness bytes are unchanged.
- Run `aiprover_000004_haiku_qwen` (stopped after 3569 s, resumable):
  formalization FAITHFUL in audit round 1; first AIProver job (82 turns, 2
  samples) ended in `sorry` and timeout; replan produced 8 lemmas; stopped
  during their AIProver jobs. The run was also interrupted once by the tool
  time limit and resumed from step 22 (pending replan).

## Todo

- Render `trace.html` automatically at the end of each run.
- Evaluate a Lean-specialized open-weights solver that fits 16 GB (for
  example Goedel-Prover-V2-8B or Kimina-Prover-Distill-8B in FP8) against
  Qwen3-4B-Instruct on `000004`.
- Resume `jiatu_000020_sonnet_haiku` (or fork `local_000020` at `prove`
  with another solver) to complete the 000020 attempt.
- Run the AIProver backend with the large model: set `api_base`/`model` in
  `aiprover/aiprover.toml`; drop the `VIBE_MODELS` override and
  `AGENT_THINKING=off` from `config_aiprover.json`.
- Replan prompt: require the captain to split a failed lemma rather than
  restate it.

## Done

- Smoke run `smoke_000004` proved and verified.
- Trace walkthrough, graph and replay pages for `smoke_000004`.
- Pluggable agent layer; local vLLM serving; run `local_000004`.
- Resumable runs with automatic restart after infrastructure failures.
- AIProver harness provisioned and integrated as the `aiprover` solver backend.

## Decisions

- Walkthrough is a single self-contained file (trace embedded as JSON); only
  KaTeX (MathML output, no stylesheet) and Google Fonts load from the network,
  with plain-text fallbacks.
- Absolute Lean attempt paths are reduced to file names in the page.
- Node heights in the replay follow their text lines; edge and phase labels
  carry a background halo so they stay legible over edges.
- Page templates in `orchestrator/` hold no run data; opened directly they
  display a notice pointing to `results/<run_id>/`.
- Replay positions are logical (stage, lemma, solver, round); time drives
  only when nodes appear and finish, so parallel chains run side by side.
- Both pages share `trace_common.css`/`trace_common.js`, inlined by the
  generator so each output file stays standalone.

- Subagent backends use the OpenAI chat schema so that a local server, a
  hosted open-weights model and a proprietary model are interchangeable;
  the orchestrator package stays standard-library only (HTTP via urllib).
- Local solver: Qwen3-4B-Instruct-2507 (Apache-2.0, already cached, 8 GB in
  bf16, non-reasoning instruct model with reliable tag-format following).
  8B Lean provers need about 16 GB in bf16 and exceed the card without
  quantization.
- vLLM runs with its native sampler (`VLLM_USE_FLASHINFER_SAMPLER=0`), which
  needs no JIT kernel build at startup.
- Tactic blocks returned with a leading `by` are normalized before splicing
  after `:= by`.

- Resume state is derived from the trace rather than a separate checkpoint
  file, so the trace remains the single record of a run.
- Captain calls on `claude_cli` time out after 2400 s (`config.json`); a
  sketch for `000020` exceeded the 900 s default.

- AIProver runs with its own configuration file through `$AIPROVER_CONFIG`;
  the plugin is used in place and not copied.

## Issues

- Solver calls in flight when a lemma is proved still complete and are
  billed; the timeline marks them as discarded (9 of 19 calls in
  `smoke_000004`).
- `local_000004`: the captain restated the same failed lemma in both replans
  instead of decomposing the induction into base and step lemmas.
- Local solvers reference constructors without their namespace
  (`ax_ITR_eps` for `Deriv.ax_ITR_eps`) and invent recursors
  (`Term.induction`); 16 of 45 checks failed on unknown identifiers.
- On SIGTERM the run waits for in-flight solver calls before unwinding;
  solver chains should observe a cancellation flag and terminate their
  `claude -p` subprocesses.
- `local_000020` was started before outcome recording on termination, so its
  trace has no outcome; it remains resumable.
- `AIProver_plugin/setup.sh` (PartitionAndProve): the embedded Python of the
  scratch `warm_text` patch has literal newlines in place of `\n` escapes and
  does not parse; the patch was applied to `aiprover/venv_mcp` directly.
- AIProver sessions are long (up to `timeout` per sample) and the local server
  holds about one full-length session; 8 lemmas at 2 samples each take over an
  hour.
