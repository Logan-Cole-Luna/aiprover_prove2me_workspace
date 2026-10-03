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

## 2026-10-02: Deployment for the AIProver model on Vista

- Repository `aiprover_prove2me_workspace` with the AIProver submodule
  (`PrithwishJana/AIProver` at `e36b1c7`) provisioned on the orchestration VM:
  Lean project `~/workspace/lean_projects/TmpProjDir` (Lean v4.23.0, Mathlib
  `37df177`, REPL, cslib `cd368e6`), `aiprover/venv_vibe`, `aiprover/venv_mcp`
  (three lean-lsp-mcp patches), ripgrep; `doctor` 13/14 PASS on both
  configurations, the endpoint row pending the server.
- `prove2me_workspace/`: clone of `prove2me/prove2me_workspace` with the same
  requires and manifest as the AIProver project and its `.lake/packages`
  linked; `Solutions.SmokeTest` builds in 4.5 s.
- JiatuBook validation set copied to `data/` (from PartitionAndProve
  `feature/SAM`); `run.py` defaults to it.
- `scripts/serve_aiprover_vista.sbatch` and `submit_aiprover_vista.sh`: Ray
  cluster across `gh` nodes in `$WORK/containers/vllm-gh200.sif`, vLLM tensor
  parallel over the nodes (default 2), Mistral tool-call and reasoning
  parsers, handoff file `$SCRATCH/servers/aiprover_server.txt`, server restart
  inside the allocation.
- `aiprover/aiprover_vista.toml` (ssh mode over `~/.ssh/vista.sock`, handoff
  followed) and `orchestrator/config_aiprover_vista.json` (captain
  `claude-opus-5-5`, auditor `claude-haiku-4-5`, solvers AIProver at 4
  samples, 100 turns, 5400 s). Captain and auditor probes answer through
  the account session (Claude Code 2.1.288 in `~/.local/bin`).
- `orchestrator/config_aiprover_vista_smoke.json`: smoke configuration with
  captain and auditor `claude-sonnet-5-5` and AIProver solvers on the base
  checkpoint `/work/11428/pjana/leanstral_hf_fused` (222.4 GiB bf16, fused
  experts): 2 samples, 60 turns, 1800 s per rollout.

## 2026-10-02: AIProver server on Vista, launch checks

- Container `$WORK/containers/vllm-gh200.sif`: vLLM 0.27.1, torch 2.13.0
  (cu130). Two-node Ray cluster and TP=2 workers form; MLA attention backend
  `FLASH_ATTN_MLA` is selected without overrides.
- Server arguments for the Leanstral checkpoints: `--tokenizer-mode mistral`
  (tekken.json only), `--config-format hf --load-format safetensors`, and
  `--limit-mm-per-prompt '{"image": 0}'` (text-only mode; the Pixtral
  processor is not profiled).
- vLLM's deepseek_v2 loader reads per-expert tensors only. The fused
  checkpoints (`leanstral_hf_fused`, `leanstral_tiny_fused`) are converted by
  `scripts/unpack_experts_vista.sbatch` (CPU `gg` node, apptainer) into
  `$SCRATCH/aiprover_ckpt/leanstral_{tiny,base}_unpacked`; config.json and
  tokenizer files are copied unchanged.
- Base checkpoint served on 2 `gh-dev` nodes (job 1044066): 68.5 GiB of
  weights per GPU with 42 GiB per rank offloaded, load 325 s, KV cache
  559k tokens (12 GiB per GPU); single-request decode about 14 tokens/s.
  `doctor` 15/15 PASS through the tunnel.
- `structures.file_scoped`: `open X in` / `set_option ... in` in a
  statement preamble become file-level commands, since the preamble precedes
  every lemma and `solution` in sketches and solutions; applied on parse and
  on resume.
- Run `vista_smoke_000004` (Sonnet 5.5 captain and auditor, AIProver base
  solvers): formalization FAITHFUL in round 0; sketch with one lemma
  (`itr_eps_all`) after the preamble fix; `itr_eps_all` proved by the
  AIProver base model (both samples verified; sample 1 in 23 turns, 1557 s).
  Solution verified: compiles, no `sorry`, no axioms, module build matches
  the theorem. Wall time 2159 s over three invocations; calls captain 11,
  auditor 1, solver 1.

## Todo

- Render `trace.html` automatically at the end of each run.
- Evaluate a Lean-specialized open-weights solver that fits 16 GB (for
  example Goedel-Prover-V2-8B or Kimina-Prover-Distill-8B in FP8) against
  Qwen3-4B-Instruct on `000004`.
- Resume `jiatu_000020_sonnet_haiku` (or fork `local_000020` at `prove`
  with another solver) to complete the 000020 attempt.
- Vista: set the AIProver checkpoint path; run `check_rl_compat.py` on it;
  submit `scripts/submit_aiprover_vista.sh <ckpt> gh-dev 2` as a launch check,
  then on `gh`; confirm `$SCRATCH` resolves to `/scratch/11757/loganluna`
  (handoff path in `aiprover_vista.toml`).
- VM: open `~/.ssh/vista.sock`; `doctor --full` with
  `aiprover/aiprover_vista.toml` (live rollout); run `vista_000004`.
- Compare `--cpu-offload-gb` and `QUANTIZATION=fp8` at 2 nodes against bf16
  at 4 nodes (throughput, solve rate).
- Replan prompt: require the captain to split a failed lemma rather than
  restate it.

## Done

- Smoke run `smoke_000004` proved and verified.
- Trace walkthrough, graph and replay pages for `smoke_000004`.
- Pluggable agent layer; local vLLM serving; run `local_000004`.
- Resumable runs with automatic restart after infrastructure failures.
- AIProver harness provisioned and integrated as the `aiprover` solver backend.
- Orchestration VM provisioned for the AIProver model on Vista; server job and
  configurations written.
- `vista_smoke_000004` proved and verified with the AIProver base model on
  Vista (Sonnet 5.5 captain).

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
- Model on Vista, everything else on the VM: compute nodes have no egress
  (captain calls) and the harness runs on the calling machine.
- Two-node TP: the bf16 checkpoint (~222 GiB) exceeds 2 x 95 GiB HBM; the
  surplus weights per rank are offloaded to Grace memory by default, which
  preserves the measured numerics; fp8 is an option.
- One Mathlib build (`~/workspace/lean_projects/TmpProjDir`) serves the
  harness and the orchestrator's workspace.
- Routing is kept as declared by the fused checkpoint's config
  (`topk_method: noaux_tc`, `scoring_func: softmax`, one group, top-4,
  normalized): transformers' `Mistral4TopkRouter` applies a softmax.
- Trained (fused) checkpoints are served after the same per-expert
  conversion.

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
- `~/workspace/autoformalization-jtemb` links to `/home/pjana`, which is not
  readable here; the Lean project is built at `~/workspace/lean_projects`.
- `/work/11428/pjana/leanstral_hf_unpacked` does not exist and the
  `/scratch/11428/pjana` copies are not readable from this account; the
  per-expert copy is produced locally.
- AIProver sessions are long (up to `timeout` per sample) and the local server
  holds about one full-length session; 8 lemmas at 2 samples each take over an
  hour.
