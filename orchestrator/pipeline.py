"""Captain/solver orchestration: formalize, audit, sketch, prove, assemble.

The orchestrator model (captain) formalizes the informal result, has the
formalization audited through a blind read-back written by a worker model,
decomposes the proof into lemmas, and assembles the final solution. Worker
models (solvers) prove the lemmas in parallel with Lean error feedback.
All Lean checking is local, in the prove2me workspace environment.
"""

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

from . import prompts
from .agents import AgentCallError, AgentPool, Completion, build_agent
from .aiprover_agent import AIProverAgent, extract_lemma_proof, split_declarations
from .lean_check import LeanChecker, forbidden_constructs, nonstandard_axioms
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
        agents = {role: build_agent(spec) for role, spec in config.agents.items()}
        system_prompts = {"captain": prompts.CAPTAIN_SYSTEM,
                          "auditor": self.auditor_system,
                          "solver": prompts.SOLVER_SYSTEM}
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
                                                   in system_prompts.items()})
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
                definitions=form.definitions, statement=form.statement,
                problems="Auditor read-back:\n" + form.readback
                         + "\n\nCaptain review:\n" + form.issues)
            last_form, form = form, None
        return last_form

    def _formalize_until_compiles(self, feedback: str) -> Formalization:
        for repair in range(self.config.max_repairs + 1):
            reply = self._captain(prompts.FORMALIZE_TEMPLATE.format(
                informal_statement=self.row["informal_statement"],
                informal_proof=self.row["informal_proof"],
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
                form = Formalization(definitions, preamble, name, signature,
                                     notes=extract_tag(reply, "notes"))
                result = self._check("formalize", self._standalone(form, form.statement),
                                     repair=repair)
                if result.ok:
                    logger.info(f"formalization compiles (repair {repair})")
                    self._decision("formalization_compiled", repair=repair,
                                   definitions=definitions, preamble=preamble,
                                   statement=form.statement, notes=form.notes)
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
        for lemma in sketch.lemmas:
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
                feedback = prompts.SOLVER_REPAIR_TEMPLATE.format(
                    helpers=helpers, proof=proof, errors="\n".join(problems))
            logger.info(f"worker {worker} exhausted its budget on {lemma.name}")
            self._decision("solver_budget_exhausted", lemma=lemma.name, worker=worker)

        with ThreadPoolExecutor(max_workers=len(pending) * self.config.workers) as pool:
            futures = [pool.submit(chain, k, lemma, worker)
                       for k, lemma in pending for worker in range(self.config.workers)]
            for future in futures:
                future.result()

    def _prove_with_aiprover(self, form: Formalization, sketch: Sketch,
                             pending: list[tuple[int, Lemma]]) -> None:
        """Prove each pending lemma with one AIProver job of `workers` samples.

        The job receives the definitions and earlier lemmas as fixed context
        and the lemma as fixed statement. Every sample's proof is extracted
        and checked by the same Lean gate as chat solvers; the first sample
        that passes proves the lemma.
        """
        solver: AIProverAgent = self.agents.agents["solver"]
        sketch_names = {lemma.name for lemma in sketch.lemmas}

        def attempt(k: int, lemma: Lemma) -> None:
            stubs = "\n\n".join(f"{earlier.statement} := by sorry"
                                 for earlier in sketch.lemmas[:k])
            context = self._standalone(form, stubs).rstrip() + "\n"
            statement = f"{lemma.statement} := by\n  sorry\n"
            fixed_names = {name for _, name, _ in split_declarations(context)}
            theorem_text = prompts.AIPROVER_LEMMA_TEMPLATE.format(
                lemma_name=lemma.name, informal_statement=self.row["informal_statement"])
            job = solver.solve(name=f"{self.run_id}_{lemma.name}"[:60],
                               theorem_text=theorem_text,
                               proof_text=self.row["informal_proof"], context=context,
                               statement=statement, samples=self.config.workers,
                               work_dir=self.temp_dir / "aiprover")
            ranked = sorted(job.samples, key=lambda sample: sample.status != "verified")
            best = next((sample.lean for sample in ranked if sample.lean), "")
            self.agents.record(
                "solver", f"{lemma.name}/aiprover", job.problem,
                Completion(text=best, error=job.error), job.seconds,
                lemma=lemma.name, worker=0, round=0, aiprover_job=job.job,
                samples=[{"sample": sample.index, "status": sample.status,
                          "turns": sample.turns, "tool_calls": sample.tool_calls,
                          "elapsed_sec": sample.elapsed_sec,
                          "problems": sample.problems[:5]} for sample in job.samples])
            errors = [job.error] if job.error else []
            for sample in ranked:
                extracted = extract_lemma_proof(sample.lean, lemma.name, fixed_names)
                if extracted is None:
                    errors.append(f"sample {sample.index} ({sample.status}): "
                                  f"no proof of `{lemma.name}` in the answer")
                    continue
                helpers, proof = extracted
                proof = strip_leading_by(proof)
                problems = [f"forbidden construct: {name}"
                            for name in forbidden_constructs(helpers + "\n" + proof)]
                problems += [f"helper `{name}` collides with a sketch lemma"
                             for _, name, _ in split_declarations(helpers)
                             if name in sketch_names]
                if not problems:
                    body = "\n\n".join(filter(None, [stubs, helpers,
                                                     f"{lemma.statement} := by\n{_indent(proof)}"]))
                    result = self._check(f"{lemma.name}_aiprover_s{sample.index}",
                                         self._standalone(form, body), lemma=lemma.name,
                                         worker=sample.index, round=0)
                    if result.ok:
                        lemma.helpers, lemma.proof, lemma.proved = helpers, proof, True
                        logger.info(f"lemma {lemma.name} proved by AIProver sample "
                                    f"{sample.index} ({sample.status})")
                        self._decision("lemma_proved", lemma=lemma.name, worker=sample.index,
                                       round=0, helpers=helpers, proof=proof)
                        return
                    problems.append(result.error_report())
                errors.append(f"sample {sample.index} ({sample.status}): "
                              + "\n".join(problems))
            lemma.last_errors = "\n\n".join(errors)[:3000]
            logger.info(f"AIProver job {job.job} did not prove {lemma.name}: "
                        f"{[sample.status for sample in job.samples]}")
            self._decision("solver_budget_exhausted", lemma=lemma.name, worker=0,
                           aiprover_job=job.job)

        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            for future in [pool.submit(attempt, k, lemma) for k, lemma in pending]:
                future.result()

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
            failed="\n\n".join(f"{lemma.statement}\nLast Lean errors:\n{lemma.last_errors}"
                               for lemma in failed))
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
            summary["status"] = "proved" if checks["verified"] else "assembly_failed"
            (self.result_dir / f"{self.slug}_standalone.lean").write_text(standalone)
            self._export_jiatu_row(standalone)
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
                               ("status", "error", "error_kind", "checks", "wall_seconds", "phase_seconds",
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


def _indent(tactics: str, width: int = 2) -> str:
    """Re-indent a tactic block uniformly under `:= by`."""
    return textwrap.indent(textwrap.dedent(tactics), " " * width)


def _declared_names(lean_text: str) -> list[str]:
    return re.findall(r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+)?(?:theorem|lemma|def|abbrev)\s+([^\s:({\[]+)",
                      lean_text, flags=re.M)
