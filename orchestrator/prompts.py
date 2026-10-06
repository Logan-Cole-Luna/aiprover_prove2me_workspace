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

FORMALIZE_LIBRARY_TEMPLATE = """Formalize the following result in Lean 4, \
building on the library below.

<library>
{library}
</library>

The library is fixed and verified: its definitions encode the setting of the \
result, and its theorems are proved. Use its definitions for every object \
they encode; do not restate, rename or redefine anything it declares. Its \
theorems may be used in the proof later; they are not part of the statement.

<informal_statement>
{informal_statement}
</informal_statement>

<informal_proof>
{informal_proof}
</informal_proof>
{feedback}
Produce:
- <definitions>: only the definitions the statement needs that the library \
lacks (often none). Only `inductive`, `structure`, `def`, `abbrev`, \
`namespace`/`end`, `open` and `notation` declarations; no theorems, no \
`sorry`, no `axiom`. Names must differ from the library's.
- <statement>: optional `open` lines, then exactly one declaration \
`theorem {theorem_name} <binders> : <type> := by sorry`, at top level (not \
inside a namespace).
- <notes>: one short paragraph mapping each part of the source statement to \
the Lean encoding, naming the library definitions used.

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
- Theorems already proved in the definitions (a library of verified \
results) may be used directly in lemmas and in the main proof; do not \
restate them as lemmas.
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
theorems and earlier lemmas given there may be used as facts.

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


# Final review and report ----------------------------------------------------

REVIEWER_SYSTEM = f"""You are an independent referee of a machine-checked \
formal proof. You did not write it and have no stake in its acceptance. Lean \
has already verified that the file compiles, contains no `sorry`, uses only \
the standard axioms, and that `solution` has exactly the type of the target \
theorem. You judge what Lean cannot: whether the target theorem and the \
definitions it depends on state the source result faithfully, and whether the \
proof establishes it by legitimate means.

{LEAN_ENV_NOTE}

Principles:
1. Read the whole setting. Every standing assumption of the source is part of \
the statement or the definitions; nothing the source proves is a hypothesis.
2. Compare hypotheses and conclusions in both directions; quantification is \
over exactly the source's objects.
3. Evaluate definitions at edge inputs. A statement that holds vacuously, or \
because a definition is degenerate (an empty relation set, a trivial group, \
a predicate that is always false), is unfaithful even though it is proved.
4. A parameter or hypothesis that replaces a concrete object of the source \
(for example, a sequence given by a formula) is acceptable only if the \
theorem then implies the source's statement; say exactly what must be \
supplied to recover it.
5. Report what you checked and what you found; do not speculate beyond the \
code. Be precise and brief."""

WRITER_SYSTEM = f"""You write the mathematical exposition of machine-checked \
proofs for research mathematicians. You state exactly what the formal code \
states, in standard notation, and give each proof at the level of a careful \
paper proof. You never claim more than the code establishes, and you write \
LaTeX that compiles with pdfLaTeX.

{LEAN_ENV_NOTE}"""

FINAL_REVIEW_TEMPLATE = """Referee the formal proof below against its source.

<informal_statement>
{informal_statement}
</informal_statement>

<informal_proof>
{informal_proof}
</informal_proof>

<target_statement>
{target_statement}
</target_statement>

<lean_solution>
{lean_solution}
</lean_solution>

Write each section in LaTeX text mode, ready to be typeset: mathematics in \
$...$ or \\[...\\], Lean identifiers as \\leanname{{name}}, no Unicode \
mathematical symbols, no Markdown, no other macros or packages.

Reply with:
<definitions_review>
Each Lean definition and the source object it encodes; any discrepancy.
</definitions_review>
<statement_review>
The target theorem against the source statement: hypotheses, quantifiers, \
conclusion, edge cases; what must be supplied to recover the source's \
statement, if anything.
</statement_review>
<proof_review>
How the formal proof is organised, which step of the source proof each part \
carries out, and where it deviates from the source proof. Note any lemma whose \
statement is stronger or weaker than the step it represents.
</proof_review>
<concerns>
An itemize list of points a mathematician should know before relying on the \
result, or the single word None.
</concerns>
<verdict>FAITHFUL, UNFAITHFUL or UNCERTAIN</verdict>"""

REPORT_TEMPLATE = """Write the mathematical text of a report that presents a \
machine-checked proof to a mathematician. The reader knows the source result \
and wants to check, without reading Lean, what was proved and how.

<informal_statement>
{informal_statement}
</informal_statement>

<informal_proof>
{informal_proof}
</informal_proof>

<lean_solution>
{lean_solution}
</lean_solution>

<declarations>
{declarations}
</declarations>

The Lean code is inserted by the report generator: write \\LeanDecl{{name}} \
where the code of a declaration listed above belongs, and never write Lean \
code yourself. \\LeanDecl{{{theorem_name}}} inserts the target statement.

Structure of <body>:
\\section{{Statement}}: the setting and the result as in the source, in clean \
LaTeX, with the same content.
\\section{{Formalization}}: each definition in mathematical terms, followed by \
its \\LeanDecl; then the target statement, \\LeanDecl{{{theorem_name}}}, and \
how it relates to the source statement, including any parameter or \
hypothesis that stands for an object of the source.
\\section{{Proof}}: \\subsection{{Overview}} relating the lemmas to the steps \
of the source proof and stating where the formal proof deviates from it; then \
every remaining declaration in the listed order, each as
  \\begin{{lemma}}[\\leanname{{name}}]\\label{{lem:name}} statement \\end{{lemma}}
  \\LeanDecl{{name}}
  \\begin{{proof}} argument \\end{{proof}}
and finally \\subsection{{Proof of the theorem}} with \\LeanDecl{{solution}} \
and its argument.

Declarations marked "library" are verified results the proof builds on, \
from earlier work: do not present them as lemmas. Introduce the library \
briefly in the Formalization section (the definitions it provides) and cite \
its theorems by name where the proof uses them; \\LeanDecl of a library \
declaration is optional.

Requirements:
- Each lemma statement says exactly what its Lean statement says: all \
hypotheses, quantifiers and types (for example, $s \\in \\mathbb{{C}}^\\times$ \
for a unit), with the same strength; not the source's informal version.
- Each proof gives the mathematical argument the Lean proof carries out, at \
the level of a careful paper proof: which earlier lemmas (\\cref{{lem:...}}) \
and which defining relations it uses, and the key computation. Do not \
narrate tactics.
- Every listed declaration appears exactly once via \\LeanDecl.
- Use only amsmath, amssymb, mathtools, amsthm environments (theorem, lemma, \
definition, remark, proof), itemize and enumerate, \\cref, \\leanname and \
\\LeanDecl. Put any \\newcommand or \\DeclareMathOperator lines in <macros>. \
No Unicode mathematical symbols; no \\usepackage, \\input or document \
environment.

Reply with:
<title>Title of the report</title>
<abstract>Three to five sentences: the result, its source, and what was \
formally verified.</abstract>
<macros>\\newcommand lines, or empty</macros>
<body>
The sections above.
</body>"""

REPORT_REPAIR_TEMPLATE = """The report text below does not compile with \
pdfLaTeX. Fix the errors and return the full corrected text in the same \
format (<title>, <abstract>, <macros>, <body>). Change nothing else.

<title>{title}</title>
<abstract>{abstract}</abstract>
<macros>{macros}</macros>
<body>
{body}
</body>

<latex_errors>
{errors}
</latex_errors>"""

REPORT_CHECK_TEMPLATE = """Below is the text of a report on a machine-checked \
proof; each Lean declaration is shown where the report places it. Check every \
lemma, definition and theorem statement in the text against the Lean code \
beside it: hypotheses, quantifiers, types, and the strength of the \
conclusion. Check that each written proof is a correct account of the \
mathematics, cites only earlier results, and claims nothing the Lean code \
does not establish. Check that the Statement section matches the source \
statement and that anything attributed to the source proof is in it.

<informal_statement>
{informal_statement}
</informal_statement>

<informal_proof>
{informal_proof}
</informal_proof>

<report>
{report}
</report>

Reply with:
<discrepancies>
Each discrepancy as an item: where it is, what is wrong, and the correction. \
Or the single word None.
</discrepancies>"""

REPORT_REVISE_TEMPLATE = """A referee compared the report text below with the \
Lean code and found the discrepancies listed. Correct each of them and return \
the full text in the same format (<title>, <abstract>, <macros>, <body>). \
Change nothing else.

<title>{title}</title>
<abstract>{abstract}</abstract>
<macros>{macros}</macros>
<body>
{body}
</body>

<discrepancies>
{discrepancies}
</discrepancies>"""
