"""Reconstruction of a run's progress from its trace, for resuming.

The trace records every completed step: compiled formalizations, audit
verdicts, accepted sketches, proved lemmas and replans. Replaying these
decisions yields the latest durable state, so an interrupted run continues
from its last completed step instead of from the beginning.
"""

from dataclasses import dataclass, field

from . import prompts
from .structures import Formalization, Sketch, normalize, parse_lemmas, parse_statement


@dataclass
class ResumeState:
    formalization: Formalization | None = None
    audited: bool = False          # the latest formalization has a verdict
    audit_rounds_done: int = 0
    formalize_feedback: str = ""   # set when the latest verdict is REVISE
    sketch: Sketch | None = None
    replans_started: int = 0
    replan_pending: bool = False   # a replan began but no new sketch was accepted
    lean_checks_done: int = 0
    previous_status: str | None = None
    restored_steps: int = 0
    proofs: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def faithful(self) -> bool:
        return (self.formalization is not None and self.audited
                and self.formalization.verdict.startswith("FAITHFUL"))

    def describe(self) -> dict:
        return {
            "restored_steps": self.restored_steps,
            "formalization": None if self.formalization is None else (
                "faithful" if self.faithful else
                "audited, revise" if self.audited else "compiled, not audited"),
            "audit_rounds_done": self.audit_rounds_done,
            "sketch_lemmas": None if self.sketch is None
                             else [lemma.name for lemma in self.sketch.lemmas],
            "lemmas_proved": [] if self.sketch is None
                             else [lemma.name for lemma in self.sketch.lemmas if lemma.proved],
            "replans_started": self.replans_started,
            "replan_pending": self.replan_pending,
            "previous_status": self.previous_status,
        }


def restore_state(document: dict) -> ResumeState:
    """Replay the decision steps of a trace document into a ResumeState."""
    state = ResumeState(restored_steps=len(document.get("steps", [])),
                        previous_status=(document.get("outcome") or {}).get("status"))
    statement_of: dict[str, str] = {}     # lemma name -> statement, current sketch
    for step in document.get("steps", []):
        if step["kind"] == "lean_check":
            state.lean_checks_done += 1
            continue
        if step["kind"] != "decision":
            continue
        event = step["event"]
        if event == "formalization_compiled":
            preamble, name, signature = parse_statement(step["statement"])
            state.formalization = Formalization(step["definitions"], step["preamble"],
                                                name, signature, notes=step.get("notes", ""))
            state.audited = False
            state.formalize_feedback = ""
        elif event == "audit_verdict" and state.formalization is not None:
            form = state.formalization
            form.verdict, form.issues = step["verdict"], step["issues"]
            form.readback = step["readback"]
            state.audited = True
            state.audit_rounds_done = step["round"] + 1
            if not form.verdict.startswith("FAITHFUL"):
                state.formalize_feedback = prompts.FORMALIZE_FEEDBACK_TEMPLATE.format(
                    definitions=form.definitions, statement=form.statement,
                    problems="Auditor read-back:\n" + form.readback
                             + "\n\nCaptain review:\n" + form.issues)
        elif event == "sketch_accepted":
            lemmas = [lemma for statement in step["lemmas"]
                      for lemma in parse_lemmas(statement + " := by sorry")]
            state.sketch = Sketch(lemmas, step["main_proof"])
            statement_of = {lemma.name: lemma.statement for lemma in lemmas}
            state.replan_pending = False
        elif event == "lemma_proved" and step["lemma"] in statement_of:
            state.proofs[normalize(statement_of[step["lemma"]])] = (step["helpers"],
                                                                    step["proof"])
        elif event == "replan":
            state.replans_started += 1
            state.replan_pending = True
    if state.sketch is not None:
        for lemma in state.sketch.lemmas:
            proof = state.proofs.get(normalize(lemma.statement))
            if proof:
                lemma.helpers, lemma.proof = proof
                lemma.proved = True
    return state
