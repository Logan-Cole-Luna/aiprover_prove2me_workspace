"""Captain/solver orchestration: formalize, audit, sketch, prove, assemble.

The orchestrator model (captain) formalizes the informal result, has the
formalization audited through a blind read-back written by a worker model,
decomposes the proof into lemmas, and assembles the final solution. Worker
models (solvers) prove the lemmas in parallel with Lean error feedback.
All Lean checking is local, in the prove2me workspace environment.
"""

import fcntl
import hashlib
import json
import logging
import re
import subprocess
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import library as libraries
from . import prompts
from .agents import AgentCallError, AgentPool, Completion, build_agent
from .aiprover_agent import (AIProverAgent, extract_lemma_proof, failed_attempt,
                             split_declarations, wrapped_lemma_proof)
from .lean_check import LeanChecker, forbidden_constructs, nonstandard_axioms
from .report import ReportBuilder, escape
from .resume import ResumeState, restore_state
from .structures import (Formalization, Lemma, Sketch, drop_imports, extract_tag,
                         normalize, parse_lemmas, parse_statement, strip_leading_by)
from .trace import Trace

logger = logging.getLogger(__name__)

FILE_HEADER = "import Mathlib\nset_option autoImplicit false\n\n"
MAIN_NAME = "solution"

# Human-readable description of the orchestration, stored in the trace header.
ALGORITHM = [
    {"stage": "formalize", "actor": "captain (orchestrator model)",
     "description": "Translate the informal statement into Lean 4 definitions and "
                    "a theorem statement ending in `sorry`. The file is compiled; "
                    "Lean errors are returned to the captain for repair."},
    {"stage": "formalize", "actor": "auditor (worker model)",
     "description": "Write a blind read-back of the Lean code, seeing only the code "
                    "and the prove2me auditor playbook, never the source."},
    {"stage": "formalize", "actor": "captain (orchestrator model)",
     "description": "Compare the read-back with the source statement and proof; "
                    "verdict FAITHFUL continues, REVISE returns to formalization."},
    {"stage": "sketch", "actor": "captain (orchestrator model)",
     "description": "Decompose the proof: lemmas stated in full and left as `sorry`, "
                    "plus a sorry-free proof of `solution` from them. The sketch must "
                    "compile."},
    {"stage": "prove", "actor": "solvers (worker model, parallel)",
     "description": "Each lemma is attacked by several independent solver chains. "
                    "A chain proposes a proof, receives Lean errors, and repairs; the "
                    "first proof that compiles is kept and the other chains stop."},
    {"stage": "sketch", "actor": "captain (orchestrator model)",
     "description": "If a lemma is not proved within budget, the captain revises the "
                    "sketch; proved lemmas with unchanged statements are reused."},
    {"stage": "assemble", "actor": "Lean",
     "description": "Splice proofs into the sketch and verify: compiles, no `sorry`, "
                    "only standard axioms, and the solution's type equals the target "
                    "theorem's type (checked in a module importing both)."},
    {"stage": "review", "actor": "reviewer (independent model)",
     "description": "Referee the verified file against the source: definitions, "
                    "statement, and the proof's correspondence with the source proof. "
                    "A run is proved only with verdict FAITHFUL."},
    {"stage": "report", "actor": "writer, reviewer",
     "description": "LaTeX report and PDF: the writer states the mathematics, the "
                    "Lean code is inserted verbatim, the reviewer checks each statement "
                    "against its Lean code, and the writer corrects the discrepancies."},
]


@dataclass
class Config:
    # Agent specification per role (captain, auditor, solver); see agents.py.
    agents: dict = field(default_factory=dict)
    workers: int = 4               # parallel solver chains per lemma
    max_repairs: int = 4           # Lean-error feedback rounds per chain
    max_replans: int = 2           # sketch revisions after failed lemmas
    max_audit_rounds: int = 2      # formalization revisions after REVISE
    lean_parallel: int = 6
    lean_timeout: int = 300
    max_claude_calls: int = 0      # calls to claude_cli agents per run; 0 = unlimited
    # AIProver lemma jobs in flight at once; 0 = all pending lemmas. Each job
    # runs `workers` sessions that share one model server.
    aiprover_lemma_concurrency: int = 0
    # AIProver jobs per lemma statement that run to completion; jobs stopped
    # by a lost model server or an interruption do not count.
    aiprover_attempts_per_lemma: int = 1
    # Sessions shared by the AIProver jobs in flight; 0 = `workers` per job.
    # Each job takes a share weighted by its lemma's failed attempts.
    aiprover_session_slots: int = 0
    # Completed failed jobs after which a lemma is handed back to the captain
    # (prove it, split it, restate it, or retry); 0 = never.
    aiprover_handback_after: int = 0
    # Claude calls added to `max_claude_calls` for the final review and the
    # report, so a run that spent its budget on proving is still reviewed.
    final_claude_calls: int = 8
    # Library of verified results the run builds on (a Lean file; see
    # library.py); overrides the problem's `library` field. With
    # `extend_library`, a reviewed proof is appended to it.
    library: str = ""
    extend_library: bool = False


def slug_from_problem(row: dict) -> str:
    """Theorem name from the book label (e.g. `prop: bounding ite base`)."""
    match = re.search(r"book label `[^:`]*:\s*([^`]+)`", row.get("informal_statement", ""))
    label = match.group(1) if match else row["uuid"]
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()


# ── Pipeline ───────────────────────────────────────────────────────────────

class Orchestration:

    def __init__(self, row: dict, config: Config, run_id: str, root: Path,
                 resume: bool = False):
        """Prepare a run. With `resume`, continue the run recorded in
        results/<run_id>/trace.json from its last completed step."""
        self.row = row
        self.config = config
        self.run_id = run_id
        self.root = Path(root)
        self.workspace = self.root / "prove2me_workspace"
        self.temp_dir = self.root / "temp" / run_id
        self.log_dir = self.root / "logs" / run_id
        self.result_dir = self.root / "results" / run_id
        for directory in (self.temp_dir, self.log_dir, self.result_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.lean = LeanChecker(self.workspace, config.lean_parallel, config.lean_timeout)
        self.slug = slug_from_problem(row)
        self.auditor_system = prompts.auditor_system(self.workspace)
        # The reviewer and the report writer default to fresh agents of the
        # captain's model.
        specs = {"reviewer": config.agents.get("captain", {}),
                 "writer": config.agents.get("captain", {}), **config.agents}
        agents = {role: build_agent(spec) for role, spec in specs.items()}
        system_prompts = {"captain": prompts.CAPTAIN_SYSTEM,
                          "auditor": self.auditor_system,
                          "solver": prompts.SOLVER_SYSTEM,
                          "reviewer": prompts.REVIEWER_SYSTEM,
                          "writer": prompts.WRITER_SYSTEM}
        trace_path = self.result_dir / "trace.json"
        previous = Trace.load(trace_path) if resume else None
        if resume and previous is None:
            raise FileNotFoundError(f"no trace to resume at {trace_path}")
        if not resume and trace_path.exists():
            raise FileExistsError(f"run {run_id} exists; resume it or choose a new run id")
        self.state = restore_state(previous) if previous else ResumeState()
        self.trace = Trace(trace_path, {
            "run_id": run_id,
            "problem": {key: row.get(key) for key in
                        ("uuid", "source_name", "source_subset", "raw_source_path",
                         "informal_statement", "informal_proof")},
            "theorem_name": self.slug,
            "models": {"orchestrator": agents["captain"].model,
                       "worker": agents["solver"].model},
            "agents": {role: agent.describe() for role, agent in agents.items()},
            "config": asdict(config),
            "algorithm": ALGORITHM,
            "lean_environment": {"toolchain": "leanprover/lean4:v4.23.0",
                                 "mathlib": "37df177aaa770670452312393d4e84aaad56e7b6",
                                 "file_header": FILE_HEADER},
            "system_prompts": system_prompts,
            "prompt_templates": {name: getattr(prompts, name) for name in dir(prompts)
                                 if name.endswith("_TEMPLATE")},
        }, previous=previous)
        self.agents = AgentPool(agents, self.log_dir / "calls.jsonl", trace=self.trace,
                                system_prompt_ids={text: key for key, text
                                                   in system_prompts.items()},
                                max_claude_calls=config.max_claude_calls)
        self.library_path = config.library or row.get("library", "")
        self.library = libraries.load(self.library_path) if self.library_path else ""
        self._file_counter = self.state.lean_checks_done
        self._counter_lock = threading.Lock()
        previous_outcome = (previous or {}).get("outcome") or {}
        self.phase_times: dict[str, float] = dict(previous_outcome.get("phase_seconds") or {})

    # Helpers -------------------------------------------------------------

    def _captain(self, prompt: str, phase: str) -> str:
        return self.agents.complete(prompt, role="captain",
                                    system_prompt=prompts.CAPTAIN_SYSTEM, phase=phase)

    def _attempt_path(self, label: str) -> Path:
        with self._counter_lock:
            self._file_counter += 1
            index = self._file_counter
        return self.temp_dir / f"{index:04d}_{label}.lean"

    def _check(self, label: str, lean_text: str, **trace_fields):
        """Elaborate a standalone file and record the check in the trace."""
        path = self._attempt_path(label)
        start = time.time()
        result = self.lean.check_text(lean_text, path)
        self.trace.add("lean_check", label=label, file=path.name, source=lean_text,
                       ok=result.ok, errors=result.errors,
                       sorry_warning=result.has_sorry_warning, axioms=result.axioms,
                       seconds=round(time.time() - start, 1), **trace_fields)
        return result

    def _decision(self, event: str, **fields) -> None:
        self.trace.add("decision", event=event, **fields)

    def _check_library(self) -> None:
        """The library must compile on its own without `sorry`."""
        result = self._check("library", FILE_HEADER + self.library + "\n")
        if not result.ok or result.has_sorry_warning:
            raise RuntimeError(f"library {self.library_path} does not compile cleanly: "
                               + (result.error_report()[:500] if not result.ok else "sorry"))

    def _own_definitions(self, form: Formalization) -> str:
        """The run's definitions without the library prefix."""
        if self.library and form.definitions.startswith(self.library):
            return form.definitions[len(self.library):].strip()
        return form.definitions

    def _library_chars(self) -> int:
        """Length of the library prefix the run's formalization was built on."""
        compiled = [step for step in self.trace.document["steps"]
                    if step.get("event") == "formalization_compiled"]
        return compiled[-1].get("library_chars", 0) if compiled else len(self.library)

    def _standalone(self, form: Formalization, body: str) -> str:
        return f"{FILE_HEADER}{form.definitions}\n\n{form.preamble}\n\n{body}\n"

    # Phase 1–2: formalize and audit -------------------------------------

    def formalize(self, form: Formalization | None = None, feedback: str = "",
                  first_round: int = 0) -> Formalization:
        """Formalize and audit until FAITHFUL or the audit budget is spent.

        A resumed run passes a compiled but unaudited `form`, or the feedback
        of its last REVISE verdict, together with the next audit round.
        """
        for audit_round in range(first_round, self.config.max_audit_rounds + 1):
            if form is None:
                form = self._formalize_until_compiles(feedback)
            lean_code = f"{form.definitions}\n\n{form.preamble}\n\n{form.statement}"
            form.readback = self.agents.complete(
                prompts.AUDITOR_TEMPLATE.format(theorem_name=form.theorem_name,
                                                lean_code=lean_code),
                system_prompt=self.auditor_system, role="auditor", phase=f"audit{audit_round}", round=audit_round)
            reply = self._captain(prompts.JUDGE_READBACK_TEMPLATE.format(
                informal_statement=self.row["informal_statement"],
                informal_proof=self.row["informal_proof"],
                lean_code=lean_code, readback=form.readback), phase=f"judge{audit_round}")
            form.verdict = extract_tag(reply, "verdict").upper()
            form.issues = extract_tag(reply, "issues")
            logger.info(f"audit round {audit_round}: verdict {form.verdict}")
            self._decision("audit_verdict", round=audit_round, verdict=form.verdict,
                           issues=form.issues, readback=form.readback)
            if form.verdict.startswith("FAITHFUL"):
                return form
            feedback = prompts.FORMALIZE_FEEDBACK_TEMPLATE.format(
                definitions=self._own_definitions(form), statement=form.statement,
                problems="Auditor read-back:\n" + form.readback
                         + "\n\nCaptain review:\n" + form.issues)
            last_form, form = form, None
        return last_form

    def _formalize_until_compiles(self, feedback: str) -> Formalization:
        for repair in range(self.config.max_repairs + 1):
            template = prompts.FORMALIZE_LIBRARY_TEMPLATE if self.library else prompts.FORMALIZE_TEMPLATE
            reply = self._captain(template.format(
                informal_statement=self.row["informal_statement"],
                informal_proof=self.row["informal_proof"], library=self.library,
                theorem_name=self.slug, feedback=feedback), phase=f"formalize{repair}")
            definitions = drop_imports(extract_tag(reply, "definitions"))
            statement_block = drop_imports(extract_tag(reply, "statement"))
            problems = [f"forbidden construct in definitions: {name}"
                        for name in forbidden_constructs(definitions)]
            try:
                preamble, name, signature = parse_statement(statement_block)
                if name != self.slug:
                    problems.append(f"the theorem must be named `{self.slug}`")
            except ValueError as e:
                problems.append(str(e))
            if not problems:
                # The library is the prefix of the definitions, so every file
                # the run checks contains it.
                full = f"{self.library}\n\n{definitions}".strip() if self.library else definitions
                form = Formalization(full, preamble, name, signature,
                                     notes=extract_tag(reply, "notes"))
                result = self._check("formalize", self._standalone(form, form.statement),
                                     repair=repair)
                if result.ok:
                    logger.info(f"formalization compiles (repair {repair})")
                    self._decision("formalization_compiled", repair=repair,
                                   definitions=form.definitions, preamble=preamble,
                                   statement=form.statement, notes=form.notes,
                                   library=self.library_path, library_chars=len(self.library))
                    return form
                problems.append("Lean errors:\n" + result.error_report())
            logger.info(f"formalization repair {repair}: {problems[0][:200]}")
            self._decision("formalization_rejected", repair=repair, problems=problems)
            feedback = prompts.FORMALIZE_FEEDBACK_TEMPLATE.format(
                definitions=definitions, statement=statement_block,
                problems="\n".join(problems))
        raise RuntimeError("formalization did not compile within the repair budget")

    # Phase 3: sketch -----------------------------------------------------

    def sketch(self, form: Formalization, replan_feedback: str = "") -> Sketch:
        feedback = replan_feedback
        for repair in range(self.config.max_repairs + 1):
            reply = self._captain(prompts.SKETCH_TEMPLATE.format(
                definitions=form.definitions, preamble=form.preamble,
                signature=form.signature, informal_proof=self.row["informal_proof"],
                feedback=feedback), phase=f"sketch{repair}")
            lemma_block = drop_imports(extract_tag(reply, "lemmas"))
            main_proof = strip_leading_by(extract_tag(reply, "main_proof"))
            lemmas = parse_lemmas(lemma_block)
            names = [lemma.name for lemma in lemmas]
            problems = [f"forbidden construct in main proof: {name}"
                        for name in forbidden_constructs(main_proof)]
            problems += [f"forbidden construct in lemma statements: {name}"
                         for name in forbidden_constructs(re.sub(r"\bsorry\b", "", lemma_block))
                         if name != "sorry"]
            if len(set(names)) != len(names) or {MAIN_NAME, form.theorem_name} & set(names):
                problems.append(f"lemma names must be unique and differ from "
                                f"`{MAIN_NAME}` and `{form.theorem_name}`")
            if not main_proof:
                problems.append("empty main proof")
            if not problems:
                candidate = Sketch(lemmas, main_proof)
                result = self._check("sketch",
                                     self._standalone(form, self._sketch_body(form, candidate)),
                                     repair=repair)
                if result.ok:
                    logger.info(f"sketch compiles with {len(lemmas)} lemma(s): {names}")
                    self._decision("sketch_accepted", repair=repair,
                                   lemmas=[lemma.statement for lemma in lemmas],
                                   main_proof=main_proof)
                    return candidate
                problems.append(result.error_report())
            logger.info(f"sketch repair {repair}: {problems[0][:200]}")
            self._decision("sketch_rejected", repair=repair, problems=problems)
            feedback = replan_feedback + prompts.SKETCH_REPAIR_TEMPLATE.format(
                lemmas=lemma_block, main_proof=main_proof, errors="\n".join(problems))
        raise RuntimeError("sketch did not compile within the repair budget")

    def _sketch_body(self, form: Formalization, sketch: Sketch,
                     use_proofs: bool = False) -> str:
        parts = []
        lemmas = dependency_order(sketch.lemmas) if use_proofs else sketch.lemmas
        for lemma in lemmas:
            if use_proofs and lemma.proved:
                if lemma.helpers:
                    parts.append(lemma.helpers)
                parts.append(f"{lemma.statement} := by\n{_indent(lemma.proof)}")
            else:
                parts.append(f"{lemma.statement} := by sorry")
        parts.append(f"theorem {MAIN_NAME}{form.signature}:= by\n{_indent(sketch.main_proof)}")
        return "\n\n".join(parts)

    # Phase 4: prove ------------------------------------------------------

    def prove(self, form: Formalization, sketch: Sketch) -> None:
        pending = [(k, lemma) for k, lemma in enumerate(sketch.lemmas) if not lemma.proved]
        if not pending:
            return
        if isinstance(self.agents.agents["solver"], AIProverAgent):
            self._prove_with_aiprover(form, sketch, pending)
            return
        solved = {lemma.name: threading.Event() for _, lemma in pending}
        result_lock = threading.Lock()

        def chain(k: int, lemma: Lemma, worker: int) -> None:
            available = "\n\n".join(f"{earlier.statement} := by sorry"
                                    for earlier in sketch.lemmas[:k]) or "(none)"
            feedback = ""
            for repair in range(self.config.max_repairs + 1):
                if solved[lemma.name].is_set():
                    return
                reply = self.agents.complete(
                    prompts.SOLVER_TEMPLATE.format(
                        definitions=form.definitions, preamble=form.preamble,
                        available_lemmas=available, lemma_statement=lemma.statement,
                        informal_proof=self.row["informal_proof"], feedback=feedback),
                    system_prompt=prompts.SOLVER_SYSTEM, role="solver", phase=f"{lemma.name}/w{worker}/r{repair}",
                    lemma=lemma.name, worker=worker, round=repair)
                helpers = drop_imports(extract_tag(reply, "helpers"))
                proof = strip_leading_by(extract_tag(reply, "proof"))
                problems = [f"forbidden construct: {name}"
                            for name in forbidden_constructs(helpers + "\n" + proof)]
                problems += [f"helper `{name}` must be named `{lemma.name}_...`"
                             for name in _declared_names(helpers)
                             if not name.startswith(lemma.name + "_")]
                if not proof:
                    problems.append("empty proof")
                if not problems:
                    body = "\n\n".join(filter(None, [available if k else "", helpers,
                                                     f"{lemma.statement} := by\n{_indent(proof)}"]))
                    result = self._check(f"{lemma.name}_w{worker}_r{repair}",
                                         self._standalone(form, body),
                                         lemma=lemma.name, worker=worker, round=repair)
                    if result.ok:
                        with result_lock:
                            if not solved[lemma.name].is_set():
                                lemma.helpers, lemma.proof, lemma.proved = helpers, proof, True
                                solved[lemma.name].set()
                                logger.info(f"lemma {lemma.name} proved by worker {worker} "
                                            f"at repair {repair}")
                                self._decision("lemma_proved", lemma=lemma.name,
                                               worker=worker, round=repair,
                                               helpers=helpers, proof=proof)
                        return
                    problems.append(result.error_report())
                else:
                    self._decision("solver_attempt_rejected", lemma=lemma.name,
                                   worker=worker, round=repair, problems=problems)
                with result_lock:
                    lemma.last_errors = "\n".join(problems)[:3000]
                    lemma.last_attempts = (
                        f"Solver {worker}, repair {repair}\n<lean>\n"
                        + "\n\n".join(filter(None, [helpers, f"{lemma.statement} := by\n{_indent(proof)}"]))
                        + "\n</lean>\nLean errors:\n" + lemma.last_errors)
                feedback = prompts.SOLVER_REPAIR_TEMPLATE.format(
                    helpers=helpers, proof=proof, errors="\n".join(problems))
            logger.info(f"worker {worker} exhausted its budget on {lemma.name}")
            self._decision("solver_budget_exhausted", lemma=lemma.name, worker=worker,
                           errors=lemma.last_errors, attempts=lemma.last_attempts)

        with ThreadPoolExecutor(max_workers=len(pending) * self.config.workers) as pool:
            futures = [pool.submit(chain, k, lemma, worker)
                       for k, lemma in pending for worker in range(self.config.workers)]
            for future in futures:
                future.result()

    def _prove_with_aiprover(self, form: Formalization, sketch: Sketch,
                             pending: list[tuple[int, Lemma]]) -> None:
        """Prove each pending lemma with AIProver jobs, up to
        `aiprover_attempts_per_lemma` completed jobs per lemma statement.

        A job receives the definitions and earlier lemmas as fixed context
        and the lemma as fixed statement. Every sample's proof is extracted
        and checked by the same Lean gate as chat solvers; the first sample
        that passes proves the lemma. Jobs share `aiprover_session_slots`
        sessions, weighted towards lemmas with more failures. After
        `aiprover_handback_after` failures a lemma is handed back to the
        captain once per statement; a split adds new lemmas to this phase.
        """
        solver: AIProverAgent = self.agents.agents["solver"]
        sketch_lock = threading.Lock()     # sketch.lemmas, statements, session counts
        sessions_in_flight: dict[str, int] = {}
        in_handback: set[str] = set()      # lemmas waiting on a captain reply
        max_attempts = max(1, self.config.aiprover_attempts_per_lemma)
        concurrency = self.config.aiprover_lemma_concurrency or len(pending)

        def weight(lemma: Lemma) -> int:
            return 1 + lemma.attempts

        def allocate(lemma: Lemma) -> int:
            """Sessions for the next job on `lemma`: its weighted share of the
            free slots against the lemmas that could start alongside it."""
            slots = self.config.aiprover_session_slots
            if not slots:
                return self.config.workers
            free = slots - sum(sessions_in_flight.values())
            waiting = sorted((other for other in sketch.lemmas
                              if other is not lemma and not other.proved
                              and other.name not in sessions_in_flight
                              and other.name not in in_handback
                              and other.attempts < max_attempts),
                             key=weight, reverse=True)
            rivals = waiting[:max(0, concurrency - len(sessions_in_flight) - 1)]
            share = free * weight(lemma) // (weight(lemma) + sum(map(weight, rivals)))
            return max(1, min(free, share))

        def maybe_handback(lemma: Lemma) -> None:
            after = self.config.aiprover_handback_after
            if (not after or lemma.proved or lemma.handed_back or lemma.attempts < after
                    or stopped.is_set()):
                return
            if not lemma.last_attempts:
                # No session left code of its own: a setup failure (clock,
                # stalled replies); retry before asking the captain.
                logger.info(f"{lemma.name}: no code in the last attempt; hand-back deferred")
            elif self.agents.claude_budget_left() < 2:
                logger.info(f"{lemma.name}: Claude budget too low for a hand-back")
            else:
                # A hand-back takes minutes against a job's 90, so the lemma
                # gives up its share of the slots meanwhile.
                with sketch_lock:
                    in_handback.add(lemma.name)
                try:
                    self._handback(form, sketch, lemma, sketch_lock, submit)
                finally:
                    with sketch_lock:
                        in_handback.discard(lemma.name)

        def attempt(lemma: Lemma) -> None:
            # The hand-back comes before the next job, so a lemma restored with
            # enough failures (after a resume) goes to the captain first.
            while not lemma.proved and not stopped.is_set():
                maybe_handback(lemma)
                if lemma.proved or lemma.attempts >= max_attempts:
                    break
                with sketch_lock:
                    samples = allocate(lemma)
                    sessions_in_flight[lemma.name] = samples
                try:
                    one_job(lemma, samples)
                finally:
                    with sketch_lock:
                        sessions_in_flight.pop(lemma.name, None)

        def context_for(lemma: Lemma) -> tuple[str, set[str]]:
            with sketch_lock:
                earlier = sketch.lemmas[:sketch.lemmas.index(lemma)]
                stubs = "\n\n".join(f"{other.statement} := by sorry" for other in earlier)
            return stubs, {name for _, name, _ in split_declarations(self._standalone(form, stubs))}

        def one_job(lemma: Lemma, samples: int) -> None:
            stubs, fixed_names = context_for(lemma)
            context = self._standalone(form, stubs).rstrip() + "\n"
            statement = f"{lemma.statement} := by\n  sorry\n"
            theorem_text = prompts.AIPROVER_LEMMA_TEMPLATE.format(
                lemma_name=lemma.name, informal_statement=self.row["informal_statement"])
            logger.info(f"AIProver job on {lemma.name}: {samples} session(s), "
                        f"attempt {lemma.attempts + 1}/{max_attempts}")
            job = solver.solve(name=f"{self.run_id}_{lemma.name}"[:60],
                               theorem_text=theorem_text,
                               proof_text=self.row["informal_proof"], context=context,
                               statement=statement, samples=samples,
                               work_dir=self.temp_dir / "aiprover")
            ranked = sorted(job.samples, key=lambda sample: sample.status != "verified")
            best = next((sample.lean for sample in ranked if sample.lean), "")
            self.agents.record(
                "solver", f"{lemma.name}/aiprover", job.problem,
                Completion(text=best, error=job.error), job.seconds,
                lemma=lemma.name, worker=0, round=0, aiprover_job=job.job,
                samples=[{"sample": sample.index, "status": sample.status,
                          "turns": sample.turns, "tool_calls": sample.tool_calls,
                          "elapsed_sec": sample.elapsed_sec, "ending": sample.ending,
                          "problems": sample.problems[:5], "check": sample.check,
                          "lean": sample.lean, "session": sample.session,
                          "reasoning": sample.reasoning} for sample in job.samples])
            errors = [job.error] if job.error else []
            with sketch_lock:
                sketch_names = {other.name for other in sketch.lemmas}
            for sample in ranked:
                extracted = extract_lemma_proof(sample.lean, lemma.name, fixed_names)
                if extracted is None:
                    errors.append(f"sample {sample.index} ({sample.status}): "
                                  f"no proof of `{lemma.name}` in the answer")
                    continue
                # The proof spliced under the sketch's statement, then, if that
                # fails, the answer's whole theorem kept as a helper.
                alias = f"{lemma.name}_aiprover_s{sample.index}"
                candidates = [("", extracted)]
                wrapped = wrapped_lemma_proof(sample.lean, lemma.name, fixed_names, alias)
                if wrapped:
                    candidates.append(("_kept", wrapped))
                problems = []
                for suffix, (helpers, proof) in candidates:
                    proof = strip_leading_by(proof)
                    found = [f"forbidden construct: {name}"
                             for name in forbidden_constructs(helpers + "\n" + proof)]
                    found += [f"helper `{name}` collides with a sketch lemma"
                              for _, name, _ in split_declarations(helpers)
                              if name in sketch_names]
                    if not found:
                        body = "\n\n".join(filter(None, [stubs, helpers,
                                                         f"{lemma.statement} := by\n{_indent(proof)}"]))
                        result = self._check(alias + suffix, self._standalone(form, body),
                                             lemma=lemma.name, worker=sample.index, round=0)
                        if result.ok:
                            lemma.helpers, lemma.proof, lemma.proved = helpers, proof, True
                            logger.info(f"lemma {lemma.name} proved by AIProver sample "
                                        f"{sample.index} ({sample.status}"
                                        f"{', own statement kept' if suffix else ''})")
                            self._decision("lemma_proved", lemma=lemma.name, worker=sample.index,
                                           round=0, helpers=helpers, proof=proof)
                            return
                        found.append(result.error_report())
                    problems += found
                errors.append(f"sample {sample.index} ({sample.status}): "
                              + "\n".join(problems))
            # A lost model server is an infrastructure failure, not a failed
            # lemma: the run stops and resumes this lemma instead of replanning.
            if not solver.endpoint_up():
                raise AgentCallError(f"AIProver endpoint unavailable during {lemma.name}")
            lemma.attempts += 1
            lemma.last_errors = "\n\n".join(errors)[:3000]
            # The samples' own code, most complete first, for the captain.
            attempts = sorted(filter(None, (failed_attempt(sample, lemma.name, fixed_names)
                                            for sample in job.samples)), key=lambda a: a[0])
            lemma.last_attempts = "\n\n".join(text for _, text in attempts)
            logger.info(f"AIProver job {job.job} did not prove {lemma.name} "
                        f"(attempt {lemma.attempts}/{max_attempts}): "
                        f"{[sample.status for sample in job.samples]}")
            self._decision("solver_budget_exhausted", lemma=lemma.name, worker=0,
                           aiprover_job=job.job, errors=lemma.last_errors,
                           attempts=lemma.last_attempts)

        # After a failure (lost endpoint, interruption), lemmas not yet
        # started are skipped rather than run against a dead server. The flag
        # is set by the failing thread itself, before its worker is reused.
        stopped = threading.Event()

        def guarded(lemma: Lemma) -> None:
            if stopped.is_set():
                return
            try:
                attempt(lemma)
            except BaseException:
                stopped.set()
                raise

        futures = []
        futures_lock = threading.Lock()

        def submit(lemma: Lemma) -> None:
            with futures_lock:
                futures.append(pool.submit(guarded, lemma))

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for _, lemma in pending:
                submit(lemma)
            try:
                done = 0
                while True:
                    with futures_lock:
                        if done == len(futures):
                            break
                        future = futures[done]
                    future.result()
                    done += 1
            except BaseException:
                stopped.set()
                raise

    def _handback(self, form: Formalization, sketch: Sketch, lemma: Lemma,
                  sketch_lock: threading.Lock, submit) -> None:
        """Hand a lemma the solvers keep failing on back to the captain.

        The captain proves it, splits it into new lemmas (handed to solvers
        through `submit`), restates it, or asks for a retry. Every proof and
        statement it proposes is checked by Lean before it is applied; one
        repair is allowed.
        """
        lemma.handed_back = True
        with sketch_lock:
            position = sketch.lemmas.index(lemma)
            listing = "\n\n".join(
                f"{other.statement} := by sorry"
                + ("  -- proved" if other.proved else "")
                + ("  -- this lemma" if other is lemma else "")
                for other in sketch.lemmas)
            earlier = sketch.lemmas[:position]
            stubs = "\n\n".join(f"{other.statement} := by sorry" for other in earlier)
            names = {other.name for other in sketch.lemmas}
            used_by_proofs = any(re.search(rf"(?<![\w.']){re.escape(lemma.name)}(?![\w'])",
                                           other.helpers + "\n" + other.proof)
                                 for other in sketch.lemmas if other.proved)
        prompt = prompts.HANDBACK_TEMPLATE.format(
            attempts=lemma.attempts, lemma_name=lemma.name, definitions=form.definitions,
            preamble=form.preamble, lemmas=listing, main_proof=sketch.main_proof,
            informal_proof=self.row["informal_proof"], failed=failed_lemma_text(lemma))
        feedback = ""
        for repair in range(2):
            if repair and self.agents.claude_budget_left() < 1:
                break
            reply = self._captain(prompt + feedback, phase=f"handback/{lemma.name}")
            diagnosis = extract_tag(reply, "diagnosis")
            helpers = drop_imports(extract_tag(reply, "helpers"))
            proof = strip_leading_by(extract_tag(reply, "proof"))
            split_block = drop_imports(extract_tag(reply, "split"))
            restated = extract_tag(reply, "restate")
            problems = []
            if restated:
                new = parse_lemmas(restated if ":=" in restated else restated + " := by sorry")
                if len(new) != 1 or new[0].name != lemma.name:
                    problems.append(f"<restate> must state exactly `theorem {lemma.name} ...`")
                elif used_by_proofs:
                    problems.append(f"proved lemmas use `{lemma.name}`; it cannot be restated")
                else:
                    with sketch_lock:
                        candidate = Sketch([new[0] if other is lemma else other
                                            for other in sketch.lemmas], sketch.main_proof)
                    result = self._check(f"{lemma.name}_restated",
                                         self._standalone(form, self._sketch_body(form, candidate)),
                                         lemma=lemma.name)
                    if result.ok:
                        old = lemma.statement
                        with sketch_lock:
                            lemma.statement = new[0].statement
                            lemma.attempts, lemma.handed_back = 0, False
                        logger.info(f"{lemma.name} restated by the captain")
                        self._decision("lemma_handback", lemma=lemma.name, action="restate",
                                       diagnosis=diagnosis, statement=lemma.statement,
                                       previous_statement=old)
                        return
                    problems.append(result.error_report())
            elif proof:
                new_lemmas = parse_lemmas(split_block) if split_block else []
                clashes = [other.name for other in new_lemmas
                           if other.name in names or other.name == lemma.name]
                problems += [f"new lemma `{name}` reuses an existing name" for name in clashes]
                problems += [f"forbidden construct: {name}"
                             for name in forbidden_constructs(helpers + "\n" + proof
                                                              + "\n" + re.sub(r"\bsorry\b", "", split_block))]
                if not problems:
                    body = "\n\n".join(filter(None, [
                        stubs, *(f"{other.statement} := by sorry" for other in new_lemmas),
                        helpers, f"{lemma.statement} := by\n{_indent(proof)}"]))
                    result = self._check(f"{lemma.name}_captain", self._standalone(form, body),
                                         lemma=lemma.name, worker="captain")
                    if result.ok:
                        with sketch_lock:
                            for offset, other in enumerate(new_lemmas):
                                sketch.lemmas.insert(position + offset, other)
                            lemma.helpers, lemma.proof, lemma.proved = helpers, proof, True
                        action = "split" if new_lemmas else "proof"
                        logger.info(f"{lemma.name}: captain {action}"
                                    + (f" into {[other.name for other in new_lemmas]}" if new_lemmas else ""))
                        self._decision("lemma_handback", lemma=lemma.name, action=action,
                                       diagnosis=diagnosis,
                                       new_lemmas=[other.statement for other in new_lemmas])
                        self._decision("lemma_proved", lemma=lemma.name, worker="captain",
                                       round=0, helpers=helpers, proof=proof)
                        for other in new_lemmas:
                            submit(other)
                        return
                    problems.append(result.error_report())
            elif "<retry" in reply:
                logger.info(f"{lemma.name}: captain asks for a retry")
                self._decision("lemma_handback", lemma=lemma.name, action="retry",
                               diagnosis=diagnosis)
                return
            else:
                problems.append("the reply names no action")
            logger.info(f"{lemma.name}: hand-back reply rejected: {problems[0][:200]}")
            feedback = prompts.HANDBACK_REPAIR_TEMPLATE.format(
                reply=reply, errors="\n".join(problems)[:6000])
        self._decision("lemma_handback", lemma=lemma.name, action="rejected",
                       diagnosis="", problems=problems[:5])

    def replan(self, form: Formalization, sketch: Sketch,
               resumed: bool = False) -> Sketch:
        """Revise `sketch` after failed lemmas. `resumed` marks the redo of a
        replan interrupted before its new sketch was accepted."""
        proved = [lemma for lemma in sketch.lemmas if lemma.proved]
        failed = [lemma for lemma in sketch.lemmas if not lemma.proved]
        feedback = prompts.REPLAN_FEEDBACK_TEMPLATE.format(
            lemmas="\n\n".join(f"{lemma.statement} := by sorry" for lemma in sketch.lemmas),
            main_proof=sketch.main_proof,
            proved="\n\n".join(lemma.statement for lemma in proved) or "(none)",
            failed="\n\n".join(failed_lemma_text(lemma) for lemma in failed))
        self._decision("replan_resumed" if resumed else "replan",
                       proved=[lemma.name for lemma in proved],
                       failed=[lemma.name for lemma in failed])
        new_sketch = self.sketch(form, feedback)
        proofs = {normalize(lemma.statement): lemma for lemma in proved}
        for lemma in new_sketch.lemmas:
            previous = proofs.get(normalize(lemma.statement))
            if previous:
                lemma.helpers, lemma.proof, lemma.proved = previous.helpers, previous.proof, True
                self._decision("lemma_reused", lemma=lemma.name)
        return new_sketch

    # Phase 5: assemble and verify ---------------------------------------

    def assemble(self, form: Formalization, sketch: Sketch) -> tuple[str, dict]:
        body = self._sketch_body(form, sketch, use_proofs=True)
        standalone = self._standalone(form, body)
        result = self._check("final", standalone + f"\n#print axioms {MAIN_NAME}\n")
        extra_axioms = nonstandard_axioms(result, MAIN_NAME)
        checks = {
            "compiles": result.ok,
            "no_sorry": not result.has_sorry_warning,
            "standard_axioms_only": extra_axioms == [],
            "axioms": result.axioms.get(MAIN_NAME),
            "errors": result.errors,
        }
        checks.update(self._verify_modules(form, body))
        checks["verified"] = all(checks[key] for key in
                                 ("compiles", "no_sorry", "standard_axioms_only",
                                  "module_build", "statement_matches"))
        self._decision("final_verification", checks=checks, solution=standalone)
        return standalone, checks

    # Phase 6–7: review and report -----------------------------------------

    def _earlier_decision(self, event: str, solution: str) -> dict | None:
        """A decision of a previous session on the same solution, if any."""
        digest = hashlib.sha256(solution.encode()).hexdigest()
        return next((step for step in reversed(self.trace.document["steps"])
                     if step.get("event") == event and step.get("solution_sha256") == digest),
                    None)

    def final_review(self, form: Formalization, standalone: str) -> dict:
        """Independent referee of the verified file against the source."""
        earlier = self._earlier_decision("final_review", standalone)
        if earlier:
            logger.info(f"final review restored: {earlier['verdict']}")
            return {key: earlier[key] for key in earlier if key in REVIEW_FIELDS}
        reply = self.agents.complete(prompts.FINAL_REVIEW_TEMPLATE.format(
            informal_statement=self.row["informal_statement"],
            informal_proof=self.row["informal_proof"],
            target_statement=f"{form.definitions}\n\n{form.preamble}\n\n{form.statement}",
            lean_solution=standalone), role="reviewer",
            system_prompt=prompts.REVIEWER_SYSTEM, phase="review")
        review = {key: extract_tag(reply, key).strip() for key in
                  ("definitions_review", "statement_review", "proof_review", "concerns")}
        review["verdict"] = extract_tag(reply, "verdict").strip().upper() or "UNCERTAIN"
        review["model"] = self.agents.agents["reviewer"].model
        logger.info(f"final review: verdict {review['verdict']}")
        self._decision("final_review", solution_sha256=hashlib.sha256(
            standalone.encode()).hexdigest(), **review)
        return review

    def credits(self, sketch: Sketch) -> dict[str, str]:
        """Who proved each declaration of the solution, from the trace."""
        steps = self.trace.document["steps"]
        solver_model = self.agents.agents["solver"].model
        credits = {}
        for lemma in sketch.lemmas:
            proved = next((step for step in reversed(steps) if step.get("event") == "lemma_proved"
                           and step.get("lemma") == lemma.name), None)
            if proved is None:
                continue
            if proved.get("worker") == "captain":
                text = "proved by the captain after a hand-back"
            else:
                jobs = [step for step in steps[:proved["index"]] if step.get("kind") == "model_call"
                        and step.get("lemma") == lemma.name and step.get("backend") == "aiprover"]
                text = (f"proved by AIProver, job {len(jobs)}, session {proved['worker']}" if jobs
                        else f"proved by solver {proved['worker']} ({solver_model}), "
                             f"round {proved.get('round', 0)}")
            credits[lemma.name] = text
            for _, helper, _ in split_declarations(lemma.helpers):
                credits[helper] = f"helper of {lemma.name}, {text}"
        credits[MAIN_NAME] = "main proof from the captain's sketch"
        return credits

    def report(self, form: Formalization, sketch: Sketch, standalone: str,
               checks: dict, review: dict) -> dict:
        """LaTeX report and PDF in results/<run_id>/report."""
        earlier = self._earlier_decision("report", standalone)
        if earlier and earlier.get("pdf") and Path(earlier["pdf"]).exists():
            logger.info("report restored")
            return {key: earlier[key] for key in ("pdf", "compiled", "discrepancies", "problems")}
        environment = self.trace.document.get("lean_environment", {})
        axioms = ", ".join(f"\\texttt{{{escape(name)}}}" for name in checks.get("axioms") or [])
        summary_rows = [
            ("Source", escape(f"{self.row.get('source_subset') or self.row.get('source_name')}, "
                              f"{self.row['uuid']}")),
            ("Lean", escape(f"{environment.get('toolchain', '')}, Mathlib "
                            f"{environment.get('mathlib', '')[:10]}")),
            ("Machine checks", "compiles; no \\texttt{sorry}; statement matches target; "
                               "module build"),
            ("Axioms", axioms),
            ("Independent review", escape(f"{review.get('verdict')} ({review.get('model')})")),
            ("Agents", escape(f"captain {self.agents.agents['captain'].model}; solvers "
                              f"{self.agents.agents['solver'].model}; report "
                              f"{self.agents.agents['writer'].model}")),
            ("Run", f"\\texttt{{{escape(self.run_id)}}}"),
        ]
        library_names = sorted(libraries.declared_names(form.definitions[:self._library_chars()]))
        if library_names:
            summary_rows.insert(1, ("Library", escape(f"{self.library_path}, "
                                                      f"{len(library_names)} declarations")))
        builder = ReportBuilder(self.result_dir / "report", self.slug, self.row,
                                form.theorem_name, form.statement, standalone,
                                self.credits(sketch), summary_rows, review, library_names)

        def complete(prompt: str, role: str, phase: str) -> str:
            return self.agents.complete(prompt, role=role, phase=phase,
                                        system_prompt=(prompts.REVIEWER_SYSTEM if role == "reviewer"
                                                       else prompts.WRITER_SYSTEM))
        try:
            record = builder.build(complete)
        except (OSError, subprocess.SubprocessError) as e:
            record = {"pdf": None, "compiled": False, "discrepancies": "",
                      "problems": [f"report tooling failed: {e}"]}
        logger.info(f"report: {'PDF written' if record['pdf'] else 'no PDF'}"
                    + (f"; {len(record['problems'])} problem(s)" if record["problems"] else ""))
        self._decision("report", solution_sha256=hashlib.sha256(standalone.encode()).hexdigest(),
                       **record)
        return record

    def _verify_modules(self, form: Formalization, body: str) -> dict:
        """Write prove2me-layout modules and check the solution against the target.

        The solution module does not import its own target theorem; a separate
        check module imports both and requires the solution's type to be the
        target's type.
        """
        def_module = f"Definitions.Def_{self.slug}"
        thm_module = f"Theorems.Thm_{self.slug}"
        sol_module = f"Solutions.Sol_{self.slug}"
        check_module = f"Solutions.Check_{self.slug}"
        files = {
            def_module: f"import Mathlib\n\n{form.definitions}\n",
            thm_module: f"import {def_module}\n\n{form.preamble}\n\n{form.statement}\n",
            sol_module: f"import {def_module}\n\n{form.preamble}\n\n{body}\n",
            check_module: (f"import {thm_module}\nimport {sol_module}\n\n{form.preamble}\n\n"
                           f"example : type_of% @{form.theorem_name} := @{MAIN_NAME}\n\n"
                           f"#print axioms {MAIN_NAME}\n"),
        }
        for module, text in files.items():
            path = self.workspace / (module.replace(".", "/") + ".lean")
            path.write_text(text)
            (self.result_dir / path.name).write_text(text)
        # Concurrent runs share the workspace's build directory; one
        # `lake build` at a time.
        with open(self.workspace / ".lake_build.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                proc = subprocess.run(["lake", "build", check_module], cwd=self.workspace,
                                      capture_output=True, text=True,
                                      timeout=self.config.lean_timeout * 2)
                output = proc.stdout + proc.stderr
                built = proc.returncode == 0
            except subprocess.TimeoutExpired:
                output, built = "lake build timed out", False
        (self.result_dir / "module_build.log").write_text(output)
        solution_imports_target = thm_module in files[sol_module]
        return {"module_build": built,
                "statement_matches": built and not solution_imports_target}

    # Driver --------------------------------------------------------------

    def run(self) -> dict:
        summary = {"run_id": self.run_id, "uuid": self.row["uuid"], "slug": self.slug,
                   "config": asdict(self.config), "status": "failed"}
        state = self.state
        form = sketch = None
        if state.restored_steps:
            logger.info(f"resuming from step {state.restored_steps}: {state.describe()}")
            self._decision("resumed", **state.describe())
        try:
            if self.library_path:
                summary["library"] = {"path": self.library_path, "chars": self._library_chars()}
            if self.library and state.formalization is None:
                self._check_library()
            if state.faithful:
                form = state.formalization
            else:
                with self._timed("formalize"):
                    form = self.formalize(
                        form=state.formalization if not state.audited else None,
                        feedback=state.formalize_feedback,
                        first_round=state.audit_rounds_done)
            summary["formalization"] = asdict(form)
            if not form.verdict.startswith("FAITHFUL"):
                summary["status"] = "unfaithful_formalization"
                return summary
            replans_used = state.replans_started
            if state.sketch is None:
                with self._timed("sketch"):
                    sketch = self.sketch(form)
            else:
                sketch = state.sketch
                if state.replan_pending:
                    with self._timed("sketch"):
                        sketch = self.replan(form, sketch, resumed=True)
            while True:
                with self._timed("prove"):
                    self.prove(form, sketch)
                if all(lemma.proved for lemma in sketch.lemmas):
                    break
                if replans_used >= self.config.max_replans:
                    summary["status"] = "lemmas_unproved"
                    return summary
                logger.info("some lemmas failed; replanning")
                with self._timed("sketch"):
                    sketch = self.replan(form, sketch)
                replans_used += 1
            with self._timed("assemble"):
                standalone, checks = self.assemble(form, sketch)
            summary["checks"] = checks
            summary["status"] = "assembly_failed"
            (self.result_dir / f"{self.slug}_standalone.lean").write_text(standalone)
            if not checks["verified"]:
                return summary
            self._export_jiatu_row(standalone)
            # A verified proof stands even if the review or the report cannot
            # be completed; its status then says so.
            summary["status"] = "review_flagged"
            if self.agents.max_claude_calls:
                self.agents.max_claude_calls = (self.agents.claude_calls
                                                + self.config.final_claude_calls)
            with self._timed("review"):
                review = self.final_review(form, standalone)
            summary["review"] = review
            if review["verdict"] == "FAITHFUL":
                summary["status"] = "proved"
            with self._timed("report"):
                summary["report"] = self.report(form, sketch, standalone, checks, review)
            if self.config.extend_library and self.library_path and summary["status"] == "proved":
                record = libraries.extend(self.library_path, standalone, self._library_chars(),
                                          form.theorem_name,
                                          f"{self.run_id} ({self.row['uuid']}), proved")
                logger.info(f"library {self.library_path}: " + (
                    f"added {len(record['declarations'])} declarations" if record["added"]
                    else f"not extended: {record['problem'][:200]}"))
                self._decision("library_extended", library=self.library_path, **record)
                summary["library_extension"] = record
            return summary
        except AgentCallError as e:
            summary.update(error=str(e), error_kind="infrastructure")
            logger.error(str(e))
            return summary
        except RuntimeError as e:
            summary.update(error=str(e), error_kind="budget")
            logger.error(str(e))
            return summary
        except (KeyboardInterrupt, SystemExit):
            summary.update(error="interrupted", error_kind="interrupted")
            logger.error("run interrupted; resume with --resume")
            raise
        finally:
            if sketch is not None:
                summary["sketch"] = {"main_proof": sketch.main_proof,
                                     "lemmas": [asdict(lemma) for lemma in sketch.lemmas]}
            summary["wall_seconds"] = self.trace.elapsed_seconds
            summary["phase_seconds"] = {k: round(v, 1) for k, v in self.phase_times.items()}
            summary["calls_by_role"] = self.agents.num_calls
            summary["usage_by_model"] = self.agents.usage_by_model
            summary["lean_attempt_files"] = self._file_counter
            (self.result_dir / "summary.json").write_text(json.dumps(summary, indent=2))
            self.trace.finish({key: summary.get(key) for key in
                               ("status", "error", "error_kind", "checks", "review", "report",
                                "wall_seconds", "phase_seconds",
                                "calls_by_role", "usage_by_model", "lean_attempt_files")})

    def _export_jiatu_row(self, standalone: str) -> None:
        """Write the result in the JiatuBook `_output.jsonl` format for evaluate.py."""
        row = dict(self.row)
        row["INFERENCE_DONE"] = True
        row["LLM_Output#1"] = f"```lean4\n{standalone}```"
        path = self.result_dir / "orchestration-zeroShot-val_JiatuBook_unlabelled_output.jsonl"
        path.write_text(json.dumps(row) + "\n")

    def _timed(self, phase: str):
        orchestration = self

        class _Timer:
            def __enter__(self):
                self.start = time.time()
                orchestration.trace.stage = phase
                logger.info(f"── phase: {phase}")

            def __exit__(self, *exc):
                orchestration.phase_times[phase] = (orchestration.phase_times.get(phase, 0.0)
                                                    + time.time() - self.start)
        return _Timer()


REVIEW_FIELDS = ("definitions_review", "statement_review", "proof_review", "concerns",
                 "verdict", "model")


def dependency_order(lemmas: list[Lemma]) -> list[Lemma]:
    """Lemmas in sketch order, except that each proved lemma follows the
    lemmas its proof uses. A proof kept across a replan may use a lemma the
    new sketch places after it. A cycle leaves the rest in sketch order."""
    def uses(lemma: Lemma, other: Lemma) -> bool:
        text = f"{lemma.helpers}\n{lemma.proof}" if lemma.proved else ""
        return re.search(rf"(?<![\w.']){re.escape(other.name)}(?![\w'])", text) is not None

    remaining, ordered = list(lemmas), []
    while remaining:
        ready = next((lemma for lemma in remaining
                      if not any(uses(lemma, other) for other in remaining if other is not lemma)),
                     remaining[0])
        remaining.remove(ready)
        ordered.append(ready)
    return ordered


def failed_lemma_text(lemma: Lemma) -> str:
    """A failed lemma as listed in the replan prompt."""
    parts = [lemma.statement, f"Last Lean errors:\n{lemma.last_errors or '(none recorded)'}"]
    if lemma.last_attempts:
        parts.append(f"<last_attempts>\n{lemma.last_attempts}\n</last_attempts>")
    return "\n".join(parts)


def _indent(tactics: str, width: int = 2) -> str:
    """Re-indent a tactic block uniformly under `:= by`."""
    return textwrap.indent(textwrap.dedent(tactics), " " * width)


def _declared_names(lean_text: str) -> list[str]:
    return re.findall(r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+)?(?:theorem|lemma|def|abbrev)\s+([^\s:({\[]+)",
                      lean_text, flags=re.M)
