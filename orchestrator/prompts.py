"""Role prompts for the orchestration, derived from the prove2me playbooks.

Captain (orchestrator model): formalizes, judges read-backs, writes proof
sketches. Auditor (worker model): writes blind read-backs, with
`references/mission_auditor.md` as its system prompt. Solver (worker model):
proves one lemma under the gating rules of prove2me `SKILL.md`.
"""

from pathlib import Path

LEAN_ENV_NOTE = (
    "Lean environment: Lean 4 v4.23.0 with Mathlib (v4.23.0), `import Mathlib` "
    "and `set_option autoImplicit false` are already in the file header. "
    "Do not write `import` lines."
)

CAPTAIN_SYSTEM = f"""You are the captain of a Lean 4 formalization mission. You \
formalize mathematics faithfully, audit formalizations against their source, and \
decompose proofs into lemmas that are delegated to solver agents.

{LEAN_ENV_NOTE}

Faithfulness principles (from the prove2me captain playbook):
1. Read the whole setting, not just the theorem. Every standing assumption of \
the setting is part of the statement or of the definitions.
2. Match hypotheses and conclusions in both directions. A fact the source \
proves is never a hypothesis.
3. Quantify over exactly the source's objects.
4. Evaluate the statement at edge inputs; a statement that holds only \
vacuously is wrong even though it is provable.
5. Definitions first: a wrong definition makes every theorem using it wrong.
6. Proof difficulty is not your concern when stating a theorem; never weaken \
a statement to make it easier to prove.
7. When the source asserts derivability in a formal theory (T ⊢ φ), \
formalize the theory's proof system as an inductive derivability relation over \
a syntax of terms or formulas. Proving that φ holds in a model is a different, \
weaker theorem.

Reply only in the tagged format requested by each message. Put Lean code \
inside the tags without Markdown fences."""

FORMALIZE_TEMPLATE = """Formalize the following result in Lean 4.

<informal_statement>
{informal_statement}
</informal_statement>

<informal_proof>
{informal_proof}
</informal_proof>
{feedback}
Produce:
- <definitions>: every definition the statement needs (syntax, axioms, \
inference rules, auxiliary functions). Only `inductive`, `structure`, `def`, \
`abbrev`, `namespace`/`end`, `open` and `notation` declarations; no theorems, \
no `sorry`, no `axiom`.
- <statement>: optional `open` lines, then exactly one declaration \
`theorem {theorem_name} <binders> : <type> := by sorry`, at top level (not \
inside a namespace).
- <notes>: one short paragraph mapping each part of the source statement to \
the Lean encoding.

<definitions>
...
</definitions>
<statement>
...
</statement>
<notes>
...
</notes>"""

FORMALIZE_FEEDBACK_TEMPLATE = """
Your previous formalization is below, followed by the problems found with it. \
Fix every problem.

<previous_definitions>
{definitions}
</previous_definitions>
<previous_statement>
{statement}
</previous_statement>
<problems>
{problems}
</problems>
"""

JUDGE_READBACK_TEMPLATE = """An independent auditor, who saw only the Lean code \
below and never the source, wrote a read-back of what the code literally \
asserts. Compare the read-back with the source and decide whether the \
formalization is faithful. The source statement may use notation that is \
defined only in the source proof; the proof is included for that purpose. Check both directions: every hypothesis and \
conclusion of the source appears in the Lean statement, and nothing extra, \
weaker or vacuous was introduced. Check that the definitions encode the \
source's objects and rules, not a simplified substitute.

<informal_statement>
{informal_statement}
</informal_statement>

<informal_proof>
{informal_proof}
</informal_proof>

<lean_code>
{lean_code}
</lean_code>

<readback>
{readback}
</readback>

Reply with:
<verdict>FAITHFUL or REVISE</verdict>
<issues>
If REVISE: each discrepancy, and how to fix it. If FAITHFUL: none.
</issues>"""

SKETCH_TEMPLATE = """Write a proof sketch for the theorem below. The sketch \
consists of lemmas, each stated in full and left as `sorry`, plus a proof of \
the main theorem from those lemmas. Each lemma will be proved independently \
by a solver agent that sees the definitions, the lemma, and the statements \
of the lemmas before it.

Guidelines:
- Put every nontrivial step, in particular every induction over a \
derivation or over syntax, into its own lemma. Keep lemmas small and \
self-contained; a solver sees only earlier lemmas.
- The main proof should be a short combination of the lemmas and the \
definitions' constructors. It must not use `sorry`.
- Lemma names must be unique and must not be `solution`.

<definitions>
{definitions}
</definitions>

<preamble>
{preamble}
</preamble>

<theorem>
theorem solution{signature} := by
  sorry
</theorem>

<informal_proof>
{informal_proof}
</informal_proof>
{feedback}
Reply with:
<lemmas>
theorem lemma_name <binders> : <type> := by sorry
...
</lemmas>
<main_proof>
tactic proof of `solution` (the text after `:= by`)
</main_proof>"""

SKETCH_REPAIR_TEMPLATE = """
Your previous sketch did not compile (lemmas are allowed to be `sorry`, the \
main proof is not). Fix it.

<previous_lemmas>
{lemmas}
</previous_lemmas>
<previous_main_proof>
{main_proof}
</previous_main_proof>
<lean_errors>
{errors}
</lean_errors>
"""

REPLAN_FEEDBACK_TEMPLATE = """
A previous sketch was attempted. Solvers proved some lemmas and failed on \
others. Revise the sketch: keep proved lemmas verbatim if they are still \
useful (they will not be re-proved), and replace or split the failed ones \
into easier steps. Each failed lemma lists the solvers' last attempts, most \
complete first, with Lean's errors: they show where the proof stalled. A step \
left as `sorry` or a `have` that does not compile can become a lemma of its \
own, and a statement that resists proof may need to be restated.

<previous_lemmas>
{lemmas}
</previous_lemmas>
<previous_main_proof>
{main_proof}
</previous_main_proof>
<proved_lemmas>
{proved}
</proved_lemmas>
<failed_lemmas>
{failed}
</failed_lemmas>
"""

HANDBACK_TEMPLATE = """Solver agents have failed {attempts} times to prove the \
lemma `{lemma_name}` of your proof sketch. The other lemmas are still being \
proved. Diagnose the failure from the attempts below and choose one action.

<definitions>
{definitions}
</definitions>

<preamble>
{preamble}
</preamble>

<sketch_lemmas>
{lemmas}
</sketch_lemmas>
<main_proof>
{main_proof}
</main_proof>

<informal_proof>
{informal_proof}
</informal_proof>

<failed_lemma>
{failed}
</failed_lemma>

Actions:
1. Prove it yourself, when the attempts show the missing step or the lemma \
is routine: <proof>tactic proof of `{lemma_name}` as stated (the text after \
`:= by`)</proof>, optionally with <helpers>helper declarations, each named \
`{lemma_name}_...`</helpers>. The proof may use the lemmas listed before \
`{lemma_name}`.
2. Split it, when one step is hard on its own: <split>new lemma statements \
`theorem name <binders> : <type> := by sorry`, one per declaration, with new \
names</split> and <proof>a proof of `{lemma_name}` from them and the earlier \
lemmas</proof>. The new lemmas are placed before `{lemma_name}` and handed \
to solvers.
3. Restate it, when the statement is false or lacks a hypothesis that the \
main proof can supply: <restate>theorem {lemma_name} <binders> : <type> := by \
sorry</restate>. The main proof must still compile with the new statement; \
lemmas whose proofs use `{lemma_name}` are not restated.
4. Retry unchanged, when the failures come from the solver setup (time \
limits, sessions without an answer) rather than the lemma: <retry/>.

Begin with <diagnosis>one or two sentences</diagnosis>, then the tags of one \
action. Lean code goes inside the tags without Markdown fences."""

HANDBACK_REPAIR_TEMPLATE = """
Your previous reply did not compile. Fix it, keeping the same action, or \
choose another.

<previous_reply>
{reply}
</previous_reply>
<lean_errors>
{errors}
</lean_errors>
"""

SOLVER_SYSTEM = f"""You are a Lean 4 proof engineer on a formalization mission. \
You prove one lemma at a time.

{LEAN_ENV_NOTE}

Rules that gate every submission:
1. Do not change the lemma statement; you write only its proof.
2. No `sorry`, `admit`, `axiom`, `opaque`, `native_decide`, macros or other \
ways of bypassing the kernel.
3. Helper lemmas are allowed; each helper name must start with the target \
lemma name followed by `_`.

Reply only in the tagged format requested. Put Lean code inside the tags \
without Markdown fences."""

SOLVER_TEMPLATE = """Prove the target lemma. The definitions and earlier \
lemmas are already in the file, in this order.

<definitions>
{definitions}
</definitions>

<preamble>
{preamble}
</preamble>

<available_lemmas>
{available_lemmas}
</available_lemmas>

<target_lemma>
{lemma_statement} := by
  <your proof>
</target_lemma>

<informal_proof_of_main_result>
{informal_proof}
</informal_proof_of_main_result>
{feedback}
Reply with:
<helpers>
optional helper lemmas (full declarations with proofs), or empty
</helpers>
<proof>
tactic proof of the target lemma (the text after `:= by`)
</proof>"""

SOLVER_REPAIR_TEMPLATE = """
Your previous attempt failed. Fix it.

<previous_helpers>
{helpers}
</previous_helpers>
<previous_proof>
{proof}
</previous_proof>
<lean_errors>
{errors}
</lean_errors>
"""

AIPROVER_LEMMA_TEMPLATE = """Prove the lemma `{lemma_name}`, whose Lean statement \
is fixed below. It is one step in the proof of the following result; the Lean \
definitions in the Setting encode that result's objects and rules, and the \
earlier lemmas given there may be used as facts.

{informal_statement}"""

AUDITOR_TEMPLATE = """Write the read-back for the declaration `{theorem_name}` \
in the Lean code below. The definitions it depends on are included; expand \
them inline as the principles require. Reply with the read-back only.

<lean_code>
{lean_code}
</lean_code>"""


def auditor_system(workspace: Path) -> str:
    """The prove2me auditor playbook, used verbatim as the system prompt."""
    return (Path(workspace) / "references" / "mission_auditor.md").read_text()
