# Logbook

## Contributions relative to prove2me

Baseline: `prove2me_workspace` (github.com/prove2me/prove2me_workspace,
9a62e67), which provides the agent skill (`SKILL.md`, role playbooks in
`references/`) and the Lean workspace layout (`Definitions/`, `Theorems/`,
`Solutions/`), and the AIProver plugin (`AIProver/`, submodule at 5d71cc5).
Neither repository is modified; the modules a run writes into
`prove2me_workspace/` are run outputs. The following are ours.

Orchestration (`orchestrator/`)
- Automated formalize, audit, sketch, prove, assemble pipeline with captain,
  auditor and solver roles; prompts derived from the prove2me playbooks.
- Pluggable agent backends: `claude -p` (subscription), OpenAI-compatible
  endpoints (local vLLM), and AIProver jobs as solver.
- AIProver integration: sample extraction and Lean gate, fallback that keeps
  the answer's own theorem under its `open` commands, helpers spliced under
  the answer's `open` commands.
- Resumable runs reconstructed from the trace; infrastructure failures
  resumed without limit, ended after three resumptions without progress.
- Claude call budget per run; per-run settings over the served config.
- Attempts per lemma statement (`aiprover_attempts_per_lemma`); interrupted
  jobs do not count.
- Weighted session sharing across AIProver jobs (`aiprover_session_slots`):
  each job takes a share of the free sessions weighted by its lemma's failed
  attempts, so freed sessions go to the remaining, harder lemmas.
- Hand-back to the captain after repeated failures
  (`aiprover_handback_after`): the captain proves the lemma, splits it into
  new lemmas, restates it, or asks for a retry; every proposal is checked by
  Lean before it is applied, while the other lemmas keep running.
- Libraries of verified results: runs build on earlier verified results
  (definitions and theorems) and extend them, from the command line or the
  query server.
- Final review by an independent referee model (status `proved` only with
  verdict FAITHFUL) and a LaTeX report with PDF per verified run: the
  mathematics written by a writer model, the Lean code inserted verbatim,
  each statement checked against its code by the referee.
- Assembly in dependency order of the proofs, so proofs kept across a
  replan precede none of the lemmas they use.
- Replan prompt with the solvers' failed code and the Lean errors located in
  it.
- Captain on Opus 5.5 with high effort and a 128,000-token reply limit.

Model serving and infrastructure (`scripts/`, `server/`, `aiprover/`)
- AIProver model served on TACC Vista (2 x GH200, vLLM, FP8 Mistral format,
  pipeline parallel), reached through an SSH ControlMaster tunnel.
- Query server: web page and JSON API, approval by token holders, on-demand
  Vista server jobs released after 15 min idle, concurrent runs, live
  progress.
- Reasoning proxy that records the model's reasoning per session and caps
  replies at 24,576 tokens, so a reply that does not converge cannot hold a
  session until its clock; it repairs malformed past tool calls that vLLM
  would reject, which restart a session.

Trace pages (`orchestrator/trace_*`)
- Walkthrough, timeline and replay of every run, embedding prompts, replies,
  Lean returns, AIProver session transcripts and the model's reasoning.
- Replay as one tree per sketch, attempts stacked under each lemma,
  hand-backs and split lemmas in place, interrupted jobs left out.

Results
- JiatuBook BoundedArithmetic 000004 proved with the AIProver models.
- G-Simple (Carbone et al., draft): Lemma 3.1, Corollary 3.2 and
  Theorem 3.3 proved and verified; Theorem 3.3 had no prior formalization.

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

## 2026-10-03: Query server

- `server/` (FastAPI, uvicorn): token-authenticated web page and JSON API on
  the VM for submitting a JiatuBook UUID or a free-text statement and proof,
  following the progress log, and retrieving the trace pages and standalone
  Lean file. Runs are `orchestrator.run` subprocesses with
  `config_aiprover_vista_served.json`.
- `Config.max_claude_calls` (`AgentPool`): budget on `claude_cli` calls per
  run, counted across resumptions; set to 10 for served runs.
- Checks: API, ownership, queueing, cancellation, adoption of a live run and
  resumption of a lost run after a server restart (stub orchestrator); budget
  stop with the real orchestrator at `--max-claude-calls 1`
  (`failed`, `budget`, one captain call).

## 2026-10-03: Approval and on-demand Vista server

- Submissions await approval by a token holder; approved runs queue.
- `server/vista.py`: the worker submits a `gh-dev` model server job when an
  approved run waits and no `aiprover_srv` job is queued or running, and
  cancels the jobs it submitted after 15 min without runs.
- A lost AIProver endpoint after a lemma attempt raises `AgentCallError`
  (`Orchestration._prove_with_aiprover`), so the run stops as an infrastructure
  failure instead of replanning; the worker requeues it and resumes it on
  the next server job (at most 3 times). Served runs use `--max-restarts 0`.
- `QUERY_SERVER_AUTH=off` for testing: no token for submission and viewing.
- Checks with stub orchestrator and stub Vista: approval (403 without
  token), one submission per demand, resumption after a lost server, idle
  cancellation.

## 2026-10-03: Trained AIProver model; submodule on main

- `AIProver` submodule updated to `origin/main` (`5d71cc5`: setup.sh
  lean-lsp-mcp patch fix, `serve/` scripts) and set to track `main`
  (`branch = main` in `.gitmodules`).
- Trained model `/work/11428/pjana/aiprover_model`: Mistral native format,
  FP8 e4m3, 112 GB. `serve_aiprover_vista.sbatch` detects `params.json` and
  serves it with the flags of `AIProver_plugin/serve/`: Mistral config and
  loader, PP across the nodes (TP=1), 1,048,576-token context, no offload,
  node-local compile caches. HF checkpoints keep the previous path.
- Query server and docs default to the trained model; scripts copied to
  `$WORK/aiprover_serve/scripts` on Vista.
- First served run `srv_20261003_142941_000000` (`lmm: Fp subset F`, base
  model): formalization FAITHFUL, sketch of 5 lemmas, none proved within
  1800 s (8 concurrent AIProver sessions at about 15 tokens/s each); replan
  to 17 lemmas, 4 proved; `lemmas_unproved` after two server-job
  resumptions.

## 2026-10-03: Served-run solver settings

- `Config.aiprover_lemma_concurrency` (AIProver lemma jobs in flight; 0 =
  all): after a failure, lemmas not yet started are skipped, the flag being
  set by the failing thread before its worker is reused.
- Served config: lemma concurrency 1, at most 5 lemmas, solver timeout
  5400 s. Checked with stub captain and solver.
- Lemma concurrency raised to 4 after the launch check (8 sessions at
  28 tokens/s each with the trained model).

## 2026-10-03: Launch check of the trained FP8 model

- Job 1046940 (`gh-dev`, 2 nodes, PP=2): weights 56.25 GiB per GPU in 52 s,
  KV cache 2,433,264 tokens, `doctor` 15/15 PASS. Decode 29 tokens/s per
  request, flat to 28 tokens/s at 8 concurrent requests (223 tokens/s
  aggregate); the base model gave 14 tokens/s single and about 12-15 at 8.
- Startup failures resolved in `serve_aiprover_vista.sbatch`: Ray's memory
  monitor killed rank 0 at 96% node memory (HBM counts toward node memory on
  GH200), now disabled with an 8 GiB object store; the kernel OOM-killed
  rank 0 while FlashInfer compiled its CUTLASS FP8 MoE kernels (one nvcc per
  core, ~4 GB each); vLLM's CUTLASS FP8 MoE is disabled for this scheme;
  `--moe-backend triton` starts cleanly.

## 2026-10-04: Served runs with the trained model

- `srv_20261003_232555_000004` (`prop: bounding ite base`): proved and
  verified (axioms `propext`), 1627 s wall; sketch of 1 lemma
  (`itr_eps_var`), proved by sample 0 in 14 turns (1092 s); sample 1 ended
  `sorry` at 1472 s and held the job until then. Calls captain 3, auditor 1,
  solver 1; 0.19 USD. Base model: 23 turns, 1557 s, 36 min over three
  invocations.
- `srv_20261003_232550_000000` (`lmm: Fp subset F`): formalization
  FAITHFUL; sketch of 2 lemmas, neither proved in 5400 s; replan to 5
  lemmas; `lemmas_unproved` after 5.4 h and two server-job resumptions.
  Proved: `rec_length_arith`, `cobhamF_length_bound` (the latter on its third
  attempt, in a resumed pass); failed: `cobhamF_append`,
  `cobhamF_smash_bound`, `cobhamF'_rec_bound`. Calls captain 4, auditor 1,
  solver 15.
- GPU use: server jobs 1046962, 1047196, 1047408 at their 2 h limit (12
  node-hours); launch checks 0.8 node-hours.

## 2026-10-04: Sketch cap removed; AIProver attempts in the trace pages

- `max_sketch_lemmas` and its prompt removed: with the instruction to prefer
  fewer, larger lemmas, the first sketch of `000000` had 2 lemmas, neither
  proved in 5400 s; the smaller lemmas of the replanned sketch were the ones
  proved.
- Trace pages: an AIProver call is one job of several samples. Its Lean
  checks and `lemma_proved` decision are attributed to the job recorded last
  before them on the same lemma (all jobs share worker 0, round 0, and the
  decision carries the sample index). Arrows between jobs read `resumed`,
  `replan` or `new job` instead of `errors`; a job without an extractable
  proof shows each sample's ending (`sorry`, `does not compile`, `no answer`,
  `server lost`) in a neutral colour.
- Each AIProver sample is drawn as its own solver lane (replay, graph,
  walkthrough timeline) with its own ending, turns, Lean check and timing.
- AIProver sample endings in the trained-model runs (30 samples): 3
  verified; 11 stopped at the 60-turn limit after 20-55 min; 12 reached the
  5400 s clock; 6 lost the server at a job's end; 1 had a tool call rejected
  by vLLM (400).

## 2026-10-04: Served settings for the 000000 retry

- Solver `max_turns` 60 → 100 (clock unchanged at 5400 s).
- Model server jobs on `gh`, 12 h (unit environment): the 2 h `gh-dev`
  limit ended sessions mid-lemma in both trained-model runs.

## 2026-10-04: AIProver sessions, Lean returns and reasoning in the replay

- The replay's step drawer, for an AIProver solver box, has the tabs Reply
  (the sample's final file), Prompt, System prompt, Lean returns (the
  harness's check of the final file with Lean's diagnostics, and the
  pipeline's check of the extracted proof), Session (the agent's transcript:
  messages, tool calls with their arguments, tool results) and Reasoning.
  Chat calls with a Lean check gain a Lean returns tab. The separate
  `trace_lean.html` page is removed.
- The model's reasoning is produced (`thinking = "high"`) but discarded by the
  harness's agent: vLLM returns it as `reasoning`, vibe reads
  `reasoning_content`. `server/reasoning_proxy.py` forwards between the
  harness and the tunnel without changes and records each completion's
  reasoning in the session's `reasoning.jsonl`; served runs use it through
  `aiprover/aiprover_vista_logged.toml`. Runs before it have no reasoning;
  the replay says so.
- The trace stores per sample the transcript (messages cut to 8,000
  characters, tool text to 6,000) and the reasoning; older traces are
  completed from `aiprover/work/jobs/` when rendered. These records are
  embedded in the replay only.

## 2026-10-04: G-Simple test problems

- `data/gsimple.jsonl` from `example/GSimple.tex` (Carbone et al., draft):
  Lemma 3.1 (X(0)=1 etc.), Corollary 3.2 (X(u)^{-1}=X(-u)) and Theorem 3.3
  (G(m) is perfect), each with the full presentation of G(m) as setting.
  Edits to the source: the proof of Lemma 3.1 cites (Re:1a) instead of the
  label of the primed relation of S(m); the commutator convention
  (a,b) = aba^{-1}b^{-1} is stated; a marked note says parts (b)-(d) of
  Lemma 3.1 follow like (a), and that c may be taken as a parameter with
  c(2) >= 1 (c is used only through E; c(2) = 21493760).
- The query server offers every JSONL file in `data/`; run ids of
  non-JiatuBook problems carry the whole problem id.
- Lemma 3.1 and Corollary 3.2 have a Lean formalization by the authors
  (Overleaf, `LeanVersion.tex`, not compiling at present).

## 2026-10-04: Concurrent served runs

- The query server runs up to two approved runs at once
  (`QUERY_SERVER_MAX_RUNS`), in submission order, never two of the same
  problem; runs adopted after a restart are followed by pid without blocking.
- AIProver `max_parallel` 8 -> 16 (machine-wide flock slots); the VM has 32
  cores and 125 GB, with 108 GB available under 8 sessions. The trained model
  served 8 concurrent requests at 28 tokens/s each; 16 is not yet measured.
- The final `lake build` of a run holds `prove2me_workspace/.lake_build.lock`.
- First concurrent pair: `srv_20261004_083722_000000` with
  `srv_20261004_123233_GSimple_Lemma_3_1`.

## 2026-10-04: Theorem 3.3 continued with 200 turns

- Solver `max_turns` 100 -> 200 (clock 5400 s): in the first sketch of
  Theorem 3.3, 12 of 22 failed samples stopped at the 100-turn limit, most with
  clock time left.
- `wrapped_lemma_proof`: if an AIProver proof does not check under the
  sketch's statement, the answer's whole theorem is kept as helper
  `<lemma>_aiprover_s<k>` (under its `open` commands) and the lemma is proved
  by `apply`. `xx_mem` sample 0 was verified by the harness but failed the
  pipeline's check (binder `hvalid` vs `h`, `Rels` under `open GM`); the kept
  form compiles.
- Theorem 3.3 stopped and resumed (step 80) with both changes; next `gh`
  model-server job 1049266 queued ahead of 1047727's end (22:37).

## 2026-10-05: Replay after a replan

- Each lemma node in the replay appears with the first accepted sketch that
  names it and hangs from that sketch. A revised sketch adds an edge,
  labelled re-delegated, to each unproved lemma it restates; reused and new
  lemmas are drawn as before.
- AIProver attempts in the replay are drawn as one block per job: a job node
  (start, duration, outcome) over its parallel sessions. Jobs on a lemma
  follow one another top to bottom; the edge into each later job names why it
  was started (resumed, replan, new job).

## 2026-10-05: Failed attempts in the replan

- The replan prompt now gives, for each failed lemma, the solvers' last code
  besides the Lean errors. For AIProver, each sample's own declarations
  (helpers and its version of the lemma, comments kept) with the Lean errors
  located in them, ordered by: some proof written, fewer errors, fewer
  `sorry`. Samples that left only `sorry` are omitted. Chat solvers give
  their last helpers and proof.
- The harness's sample status is not shown to the captain: it counts the
  `sorry` stubs of earlier lemmas in the sample's file.
- `solver_budget_exhausted` decisions record the errors and attempts;
  resuming restores them, so a replan after an interruption sees them.
- AIProver jobs themselves are unchanged (no feedback from earlier jobs).

## 2026-10-05: Resumptions without limit; interrupted jobs out of the pages

- A served run that loses its model server is requeued and resumed without a
  limit on resumptions. It ends after 3 resumptions in a row that add no
  durable progress (formalization, verdict, sketch, proved lemma, replan),
  counted from the trace (`progress`, `stalled` columns of `jobs`).
- The trace pages leave out AIProver jobs stopped by an interruption: jobs
  with neither a proved lemma nor a recorded failure, or with every session
  cancelled, when the run later resumed. Their Lean checks are left out too;
  the later job on the lemma takes their place and steps are renumbered.
  `trace.json` keeps every step.

## 2026-10-05: Attempts per lemma; Opus captain; Theorem 3.3 continued

- `aiprover_attempts_per_lemma` (default 1): a lemma is retried by new
  AIProver jobs until proved or until that many jobs on its statement ran to
  completion. Jobs stopped by a lost server or an interruption do not count.
  Counts are restored from the trace on resume, so a resumption does not
  re-run a lemma whose attempts are used up. Replay label: retry.
- Served config: captain Opus 5.5 (auditor Sonnet 5.5).
- Per-run settings in the server (`options` column, JSON).
- Theorem 3.3 after 42,326 s: 23/26 lemmas proved, `lemmas_unproved`
  (yy_mem 4 completed jobs, h1_cube_eq 2, h2_mem 2). Requeued as a resume
  with 5 attempts per lemma and `max_replans` 2.
- An AIProver answer's helper declarations are spliced under the answer's
  `open` commands (`open GM in`), as its kept theorem already was. The last
  `h2_mem` sample was rejected for unknown identifiers (`Valid`, `Rels`,
  `xx`) in its helpers; with the fix its remaining errors are its own: it
  rewrote the fixed context with `c` implicit and calls the context lemmas
  without `c`.

## 2026-10-05: Replay per sketch; stalled AIProver replies

- The replay draws one tree per accepted sketch, stacked downward: the
  sketch fans out left to right into its lemmas, and the attempts on each
  lemma stack downward beneath it (AIProver: a job block with its sessions
  below, each later job under the previous one; chat solvers: one lane per
  chain). A replan starts the next tree below the previous one; lemmas it
  keeps appear as single nodes, and the edge to the replan runs down the
  left of the previous tree.
- Chat-solver Lean checks are matched to the latest call with the same
  lemma, worker and round; checks of later sketches had been attributed to
  the first sketch's calls.
- `yy_mem` (Theorem 3.3): of 10 sessions, 8 ran to the clock or turn limit
  with a final reply 1-4 min before the end; 2 stopped receiving replies
  after 2 and 21 turns and waited 83 and 77 min on one generation. Six
  sessions of the run show the same stall (61-83 min). Agent requests carry
  no `max_tokens`, so one reply may run to the context limit (1,048,576).
- The reasoning proxy sets `max_tokens = 24576` on chat requests without
  one. Of 10,823 recorded replies the 99.9th percentile is 57,000
  characters (~16,000 tokens) and the longest 190,000; the cap takes about
  14 min at 29 tokens/s. A capped reply without a tool call ends the
  agent's turn and the harness's answer check returns the Lean errors (up
  to 3 times per session). Capped replies are marked `truncated` in
  `reasoning.jsonl` and in the replay's Reasoning tab.
- Theorem 3.3 queued with `max_replans` 3: two replans by the Opus captain
  after the Sonnet one.

## 2026-10-05: Session sharing and hand-back to the captain

- `aiprover_session_slots` (served: 8): sessions per AIProver job are
  `free * w / (w + sum of rivals' w)`, at least 1, where `w = 1 + failed
  attempts` and the rivals are the unproved lemmas that could start
  alongside it (up to `aiprover_lemma_concurrency`). A mocked run of two
  lemmas gave 6 and 2 sessions, then 8 to the last one.
  A lemma waiting on a hand-back is not a rival; if its hand-back fails
  with no free slot, its job takes 1 session above the limit.
- `aiprover_handback_after` (served: 3): before a lemma's next job, once per
  statement, the captain receives the sketch, the informal proof and the
  failed attempts and chooses: prove (helpers and proof checked by Lean),
  split (new lemmas placed before it and handed to solvers, with a proof of
  it from them), restate (the sketch must compile with the new statement;
  refused if proved lemmas use it), or retry. One repair on Lean errors. A
  hand-back is deferred when no session left code (setup failure) and
  skipped when fewer than 2 Claude calls remain. `lemma_handback` decisions
  are restored on resume. Tested on Theorem 3.3's state: the captain path
  accepted a proof of `h2_mem` (rw [h2_eq_conj c s]; conjugation in the
  normal commutator subgroup), refused a restatement of `h1_cube_eq` used by
  `h1_mem`, and applied a split.
- Captain: `effort = "high"`, `max_output_tokens = 128000`
  (`CLAUDE_CODE_MAX_OUTPUT_TOKENS`); no token cap of ours applies to it.
- Pages: jobs with different session counts, hand-back nodes in the lemma's
  column, split lemmas hanging from their hand-back, captain proofs as
  winners.
- Theorem 3.3 queued with `max_claude_calls` 16.

## 2026-10-06: Libraries of verified results

- `orchestrator/library.py`; `Config.library`, `Config.extend_library`.
  A run given a library formalizes its statement against the library's
  definitions (`FORMALIZE_LIBRARY_TEMPLATE`); the library text is the prefix
  of the definitions, so lemma checks, solver contexts and the final file
  contain it. `formalization_compiled` records the library and its length;
  the run's contribution is the text after that prefix. A library is checked
  to compile without `sorry` at the start of a run. Reports cite library
  declarations and do not present them.
- `libraries/gsimple.lean`: definitions, lemmas and `gsimple_g_m_perfect`
  from the Theorem 3.3 run (45 declarations), checked by Lean.
- Query server: `library`, `library_text`, `extend_library` and `after` on
  submission; `GET /api/libraries`; the form selects or uploads a library.
  The worker starts a run with `after` only once that run has ended.
- Theorems 3.4, 3.6, 3.7 and Lemma 3.5 run in sequence on the library, each
  extending it; their notes allow citing earlier results the library
  provides.

## 2026-10-06: G-Simple §3.1 problems

- `data/gsimple.jsonl`: Theorem 3.4 (the toral subgroup $H$ normalizes the
  unipotent subgroup $U$), Lemma 3.5 ($uh = hu'$), Theorem 3.6
  ($G(\mathfrak m) = HU$) and Theorem 3.7 ($Z(G(\mathfrak m)) \le H$), with the
  same setting as 3.1 to 3.3 and the definitions of $H$, $U$ and the center
  from §3.1. Each cited earlier result is stated in a marked note and must
  be proved within the solution. Theorem 3.7 has no proof in the source.
- Submitted through the query server with the defaults (Opus 5.5
  orchestrator, reviewer and writer; trained AIProver model), 5 attempts per
  lemma, 2 replans, 16 Claude calls.

## 2026-10-06: Final review, report, replay spacing

- Stages `review` and `report` after a verified assembly
  (`orchestrator/pipeline.py`, `orchestrator/report.py`). Roles `reviewer`
  and `writer`, served as `claude-opus-5-5` with high effort, each a fresh
  agent with its own system prompt. Both decisions are restored on resume
  for the same solution (SHA-256 of the file), so a resumed run does not
  repeat them. `final_claude_calls` (8) is added to the call budget.
- Report: the writer's text names declarations with `\LeanDecl{name}`; the
  generator inserts the declaration from the verified file (with its doc
  comment), highlighted by Pygments, and its credit from the trace. Checks
  before compiling: every declaration placed exactly once, no file, shell or
  preamble commands. pdfLaTeX, since LuaTeX's font loader is not installed
  on the VM; Unicode symbols of the Lean code are declared as LaTeX symbols
  (unmapped ones are boxed and reported). Two repair rounds on LaTeX errors.
- Replay: a lemma column is as wide as the most sessions of its own jobs;
  jobs of more than 4 sessions wrap into offset rows. Review and report
  nodes follow the verification node.
- Query server: `report.pdf` and `report.tex` among a run's files.

## 2026-10-06: Theorem 3.3 proved

- `h1_cube_eq` proved by AIProver sample 7 of an 8-session job (its third
  attempt). Assembly placed `ww_mem`, whose proof was kept from the first
  sketch, before `xx_mem` and `yy_mem`, which it uses; the assembled file
  now orders proved lemmas after the lemmas their proofs reference
  (`dependency_order`). The resumed assembly verified: compiles, no sorry,
  standard axioms, module build, statement match. 26 lemmas, 2 sketches,
  calls 7 / 1 / 45 (captain / auditor / solver), 15.3 h of run time.

## 2026-10-06: Theorem 3.3, 25 of 26 lemmas

- Resumed on a 2 h `gh-dev` server with the Opus captain. The hand-back
  proved `yy_mem` after one repair (from `yy_conj_mem`, `yy_sub_mk` and
  t·v − v = (t − 1)·v); AIProver proved `h2_mem` (conjugate of `h1_mem` by
  `wm 1` in the normal commutator subgroup). `h1_cube_eq` remains.
- No reply reached the proxy's token cap.
- The 2 h server ended during `h1_cube_eq`'s third job (not counted). One
  session had restarted at 29 min after vLLM rejected a request (400): an
  earlier tool call of the model had arguments that were not JSON. The
  restart reset the session clock, so the job outlived the server. Such
  rejections occurred in 5 of 638 sessions; the proxy now repairs them.
- Lemmas waiting on a hand-back are no longer counted in the session split.

## Todo


- On SIGTERM, cancel the run's AIProver jobs (`aiprover cancel`) so it
  unwinds at once; it now waits for in-flight jobs (up to 5400 s).

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
- Query server deployment: tokens, systemd user unit, linger, port 8443 in
  the VM security group; a certificate for HTTPS.
- Resume re-proves every unproved lemma, including lemmas that already failed
  in the current sketch; `cobhamF_length_bound` was proved on such a retry.
  Make retries an explicit per-lemma attempt budget instead.
- Stop the remaining AIProver samples of a lemma once one sample verifies.

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
- Query server with run queue, live progress and Claude call budget.
- Per-run model choice for orchestrator and subagent (Claude model and
  reasoning level, or AIProver trained/base) on the query server page.

## Decisions

- A run uses one AIProver version for both roles; the worker runs together only
  the runs that share the served checkpoint, and a waiting run of another
  version holds back later runs that need a model server.
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

- Query server runs one job at a time: runs share the modules of
  `prove2me_workspace/` and the Vista server (about 14 tokens/s per request).
- Served runs are subprocesses of `orchestrator.run`, so tracing, resumption
  and outputs are those of a manual run.
- Authentication by per-user bearer tokens; the web page holds the token in an
  HttpOnly cookie, which also authorizes the event stream and result links.

## Issues

- The AIProver orchestrator (OpenAI-compatible, 24,576 reply tokens) and the
  per-checkpoint server switching are untested on Vista.
- The served budget of 10 Claude calls is below the 12 used by
  `vista_smoke_000004`; runs with sketch repairs or replans stop before
  assembly.

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
