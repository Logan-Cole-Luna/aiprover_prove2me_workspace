"""LaTeX report and PDF of a verified solution, for a mathematician.

The writer model supplies the mathematical text: the statement,
each definition and lemma in standard notation, and the argument of each
proof. The Lean code is never retyped by a model: the text names a
declaration with `\\LeanDecl{name}` and the generator inserts that
declaration verbatim from the verified file, highlighted by Pygments, with
the credit of the agent that proved it. The reviewer then checks every
statement in the text against the Lean code beside it, and the writer
corrects the discrepancies once. The document is compiled with pdfLaTeX
(no shell escape), with the Unicode symbols of the Lean code mapped to LaTeX
symbols; compile errors go back to the writer for repair.

Rebuild a report from its saved parts without model calls:
    python3 -m orchestrator.report results/<run_id>/report
"""

import json
import re
import subprocess
import unicodedata
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from pygments import highlight
from pygments.formatters import LatexFormatter
from pygments.lexers import get_lexer_by_name

from . import prompts
from .aiprover_agent import _BLOCK_END, _DECL_START
from .structures import extract_tag

MAX_COMPILE_REPAIRS = 2
# Constructs the writer's text may not contain: they would read or write
# files, run code, or replace the document's preamble.
FORBIDDEN_LATEX = re.compile(
    r"\\(?:input|include|write|immediate|openout|openin|read|directlua|luaexec|"
    r"usepackage|RequirePackage|documentclass|catcode|special|ShellEscape)\b"
    r"|\\(?:begin|end)\{document\}")
DOC_COMMENT = re.compile(r"(?:/--(?:(?!-/).)*-/\s*|open [^\n]+ in\s*)+$", re.S)

PREAMBLE = r"""\documentclass[11pt]{article}
\usepackage[margin=1in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[varqu,scaled=0.95]{zi4}
\usepackage{amsmath,amssymb,amsthm,mathtools}
\usepackage[dvipsnames]{xcolor}
\usepackage{fvextra}
\usepackage{booktabs,enumitem,microtype,needspace}
\usepackage[colorlinks,linkcolor=MidnightBlue,citecolor=MidnightBlue,urlcolor=MidnightBlue]{hyperref}
\usepackage[capitalise,nameinlink]{cleveref}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{lemma}[theorem]{Lemma}
\theoremstyle{definition}
\newtheorem{definition}[theorem]{Definition}
\theoremstyle{remark}
\newtheorem{remark}[theorem]{Remark}
\newcommand{\leanname}[1]{\texttt{\detokenize{#1}}}
\newenvironment{leanblock}[1]
  {\par\smallskip\Needspace{4\baselineskip}\noindent{\footnotesize\textsf{#1}}\par\nopagebreak\vspace{2pt}}
  {\par\medskip}
\fvset{breaklines=true,breakanywhere=true,fontsize=\footnotesize,frame=leftline,
  framerule=0.8pt,rulecolor=\color{gray!60},xleftmargin=4pt,framesep=6pt}
"""


# Unicode in Lean code, as pdfLaTeX math symbols. Blackboard, script and
# Fraktur letters are mapped from their Unicode names in
# `unicode_declarations`.
UNICODE_SYMBOLS = {
    "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow", "↦": r"\mapsto",
    "⇑": r"\Uparrow", "↑": r"\uparrow", "∀": r"\forall", "∃": r"\exists",
    "⟨": r"\langle", "⟩": r"\rangle", "≤": r"\le", "≥": r"\ge", "≠": r"\ne",
    "∈": r"\in", "∉": r"\notin", "⊤": r"\top", "⊥": r"\bot", "⊢": r"\vdash",
    "·": r"\cdot", "∘": r"\circ", "×": r"\times", "∧": r"\wedge", "∨": r"\vee",
    "¬": r"\neg", "ℓ": r"\ell", "∑": r"\sum", "∏": r"\prod", "⊆": r"\subseteq",
    "⊂": r"\subset", "∩": r"\cap", "∪": r"\cup", "≃": r"\simeq", "≅": r"\cong",
    "≡": r"\equiv", "∣": r"\mid", "‖": r"\|", "∅": r"\emptyset", "∞": r"\infty",
    "⋯": r"\cdots", "…": r"\ldots", "•": r"\bullet", "⁅": r"[\![", "⁆": r"]\!]",
    "⦃": r"\{\!|", "⦄": r"|\!\}", "⧸": "/", "ˣ": r"^{\times}", "⁻": "^{-}",
    "⁺": "^{+}", "¹": "^{1}", "²": "^{2}", "³": "^{3}",
    **{chr(0x2070 + k): f"^{{{k}}}" for k in (0, 4, 5, 6, 7, 8, 9)},
    **{chr(0x2080 + k): f"_{{{k}}}" for k in range(10)},
    **{letter: "\\" + name for letter, name in zip(
        "αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ",
        "alpha beta gamma delta varepsilon zeta eta theta iota kappa lambda mu nu xi "
        "pi rho sigma tau upsilon varphi chi psi omega Gamma Delta Theta Lambda Xi Pi "
        "Sigma Phi Psi Omega".split())},
}
FONT_SHAPES = {"DOUBLE-STRUCK": r"\mathbb", "SCRIPT": r"\mathcal", "FRAKTUR": r"\mathfrak"}


def unicode_declarations(text: str) -> tuple[str, list[str]]:
    """`\\DeclareUnicodeCharacter` lines for the non-ASCII characters of
    `text`, and the characters without a mapping (typeset as a boxed code
    point)."""
    lines, unmapped = [], []
    for char in sorted({c for c in text if ord(c) > 127}):
        symbol = UNICODE_SYMBOLS.get(char)
        name = unicodedata.name(char, "")
        shape = next((macro for key, macro in FONT_SHAPES.items() if key in name), None)
        letter = name.rsplit(" ", 1)[-1]
        if symbol is None and shape and len(letter) == 1:
            symbol = f"{shape}{{{letter.lower() if 'SMALL' in name else letter}}}"
        code = f"{ord(char):04X}"
        if symbol is None:
            unmapped.append(char)
            lines.append(f"\\DeclareUnicodeCharacter{{{code}}}{{\\fbox{{\\tiny U+{code}}}}}")
        else:
            lines.append(f"\\DeclareUnicodeCharacter{{{code}}}{{\\ensuremath{{{symbol}}}}}")
    return "\n".join(lines), unmapped


@dataclass
class ReportText:
    title: str
    abstract: str
    macros: str
    body: str

    @staticmethod
    def parse(reply: str) -> "ReportText":
        return ReportText(*(extract_tag(reply, tag) for tag in ("title", "abstract", "macros", "body")))


def lean_declarations(lean_text: str) -> list[tuple[str, str, str]]:
    """Top-level declarations as (kind, name, source text), with the doc
    comment or `open ... in` directly above each one."""
    starts = list(_DECL_START.finditer(lean_text))
    begins = []
    for match in starts:
        above = DOC_COMMENT.search(lean_text[:match.start()])
        begins.append(above.start() if above else match.start())
    declarations = []
    for k, match in enumerate(starts):
        end = begins[k + 1] if k + 1 < len(starts) else len(lean_text)
        terminator = _BLOCK_END.search(lean_text, match.end(), end)
        if terminator:
            end = terminator.start()
        declarations.append((match.group(1), match.group(2),
                             lean_text[begins[k]:end].strip()))
    return declarations


def highlighted(lean_source: str) -> str:
    return highlight(lean_source, get_lexer_by_name("lean4"),
                     LatexFormatter(verboptions="breaklines=true"))


def plain_lean_names(latex: str) -> str:
    """`\\leanname{a\\_b}` → `\\leanname{a_b}`: the macro prints its argument
    verbatim, so an escaped underscore would show its backslash."""
    return re.sub(r"\\leanname\{([^}]*)\}",
                  lambda m: "\\leanname{" + m.group(1).replace("\\_", "_") + "}", latex)


def escape(text: str) -> str:
    return re.sub(r"([#$%&_{}])", r"\\\1", text).replace("~", r"\textasciitilde{}")


class ReportBuilder:
    """Builds, checks and compiles the report of one verified run."""

    def __init__(self, out_dir: Path, slug: str, row: dict, theorem_name: str,
                 target_statement: str, solution: str, credits: dict[str, str],
                 summary_rows: list[tuple[str, str]], review: dict,
                 library_names: list[str] | None = None):
        self.out_dir = Path(out_dir)
        self.slug = slug
        self.row = row
        self.theorem_name = theorem_name
        self.target_statement = target_statement
        self.solution = solution
        self.credits = credits
        self.summary_rows = summary_rows
        self.review = review
        # Declarations of the library the run built on: cited, not presented.
        self.library_names = list(library_names or [])
        self.declarations = lean_declarations(solution)
        self.lean_code = {name: text for _, name, text in self.declarations}
        statement = re.sub(r":=\s*by\s+sorry\s*$", "", target_statement.strip())
        self.lean_code[theorem_name] = statement
        self.unmapped: list[str] = []

    # Inputs and checks ---------------------------------------------------

    def declaration_list(self) -> str:
        lines = [f"{self.theorem_name} (target statement)"]
        library = set(self.library_names)
        lines += [f"{name} ({kind}; library, verified earlier)" if name in library
                  else f"{name} ({kind}; {self.credits.get(name, 'definition')})"
                  for kind, name, _ in self.declarations]
        return "\n".join(lines)

    def problems(self, text: ReportText) -> list[str]:
        """Contract violations of the writer's text, before compiling."""
        problems = []
        for part in (text.macros, text.body, text.title, text.abstract):
            problems += [f"forbidden construct `{m.group(0)}`" for m in FORBIDDEN_LATEX.finditer(part)]
        used = re.findall(r"\\LeanDecl\{([^}]*)\}", text.body)
        unknown = sorted(set(used) - set(self.lean_code))
        missing = [name for name in self.lean_code
                   if name not in used and name not in self.library_names]
        repeated = sorted({name for name in used if used.count(name) > 1})
        if unknown:
            problems.append(f"\\LeanDecl names not in the declaration list: {', '.join(unknown)}")
        if missing:
            problems.append(f"declarations without \\LeanDecl: {', '.join(missing)}")
        if repeated:
            problems.append(f"declarations placed more than once: {', '.join(repeated)}")
        if not text.body.strip():
            problems.append("empty <body>")
        return problems

    # Document --------------------------------------------------------------

    def caption(self, name: str) -> str:
        if name == self.theorem_name:
            return (f"Lean: target statement \\texttt{{{escape(name)}}}, fixed before the "
                    "proof; \\texttt{solution} is checked to have exactly this type")
        credit = ("library, verified earlier" if name in self.library_names
                  else self.credits.get(name))
        return f"Lean: \\texttt{{{escape(name)}}}" + (f" --- {escape(credit)}" if credit else "")

    def lean_block(self, name: str) -> str:
        return (f"\\begin{{leanblock}}{{{self.caption(name)}}}\n"
                f"{highlighted(self.lean_code[name])}\\end{{leanblock}}")

    def review_section(self, plain: bool = False) -> str:
        """The referee's findings, inserted verbatim (as plain text with
        `plain`, if they do not compile)."""
        if not self.review.get("verdict"):
            return ""
        parts = ["\\section{Independent review}",
                 f"A referee model ({escape(self.review.get('model', ''))}), which took no part "
                 "in the formalization or the proof, compared the verified Lean file with the "
                 f"source. Verdict: \\textbf{{{escape(self.review['verdict'])}}}."]
        for key, heading in (("definitions_review", "Definitions"),
                             ("statement_review", "Statement"),
                             ("proof_review", "Proof"), ("concerns", "Concerns")):
            if self.review.get(key):
                finding = escape(self.review[key]) if plain else plain_lean_names(self.review[key])
                parts.append(f"\\paragraph{{{heading}.}} {finding}")
        return "\n\n".join(parts)

    def summary_table(self) -> str:
        rows = "\n".join(f"{escape(key)} & {value} \\\\" for key, value in self.summary_rows)
        return ("\\begin{center}\\small\n\\begin{tabular}{@{}ll@{}}\n\\toprule\n"
                f"{rows}\n\\bottomrule\n\\end{{tabular}}\n\\end{{center}}")

    def document(self, text: ReportText, plain_review: bool = False) -> str:
        body = re.sub(r"\\LeanDecl\{([^}]*)\}",
                      lambda m: self.lean_block(m.group(1)) if m.group(1) in self.lean_code
                      else m.group(0), plain_lean_names(text.body))
        review = self.review_section(plain=plain_review)
        style = LatexFormatter().get_style_defs()
        unicode, self.unmapped = unicode_declarations(self.solution + self.target_statement)
        appendix = ("\\appendix\n\\section{Complete Lean file}\n"
                    "The verified file, as checked by Lean.\n\n" + highlighted(self.solution))
        return (f"{PREAMBLE}{style}\n{unicode}\n{text.macros}\n\n"
                f"\\title{{{text.title}}}\n\\date{{}}\n\\begin{{document}}\n\\maketitle\n"
                f"\\begin{{abstract}}\n{text.abstract}\n\\end{{abstract}}\n\n"
                f"{self.summary_table()}\n\n\\tableofcontents\n\n{body}\n\n{review}\n\n"
                f"{appendix}\n\\end{{document}}\n")

    def compile(self, document: str) -> tuple[bool, str]:
        """Compile with pdfLaTeX (twice, for references); return (ok, errors)."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tex = self.out_dir / f"{self.slug}.tex"
        tex.write_text(document)
        for _ in range(2):
            try:
                proc = subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                     "-no-shell-escape", tex.name],
                    cwd=self.out_dir, capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                return False, "pdflatex timed out"
            if proc.returncode != 0:
                return False, latex_errors(self.out_dir / f"{self.slug}.log")
        return True, ""

    def report_with_code(self, text: ReportText) -> str:
        """The body with each declaration's Lean code inline, for the check."""
        return re.sub(r"\\LeanDecl\{([^}]*)\}",
                      lambda m: f"[Lean code of {m.group(1)}]\n```lean\n"
                                f"{self.lean_code.get(m.group(1), '(unknown)')}\n```", text.body)

    def save(self, text: ReportText) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        parts = {"builder": {key: value for key, value in vars(self).items()
                             if key in ("slug", "row", "theorem_name", "target_statement",
                                        "solution", "credits", "summary_rows", "review",
                                        "library_names")},
                 "text": asdict(text)}
        (self.out_dir / "report_parts.json").write_text(json.dumps(parts, indent=1, ensure_ascii=False))

    # Writer loop -------------------------------------------------------------

    def build(self, complete: Callable[[str, str, str], str]) -> dict:
        """Write, check and compile the report; `complete(prompt, role, phase)`
        calls a model. Returns a record of the result."""
        record = {"pdf": None, "compiled": False, "discrepancies": "", "problems": []}
        reply = complete(prompts.REPORT_TEMPLATE.format(
            informal_statement=self.row["informal_statement"],
            informal_proof=self.row["informal_proof"], lean_solution=self.solution,
            declarations=self.declaration_list(), theorem_name=self.theorem_name),
            "writer", "report_write")
        text = ReportText.parse(reply)
        text, ok, errors = self._compile_with_repairs(text, complete, "report_repair")
        record["compiled"] = ok
        if ok:
            check = complete(prompts.REPORT_CHECK_TEMPLATE.format(
                informal_statement=self.row["informal_statement"],
                informal_proof=self.row["informal_proof"],
                report=self.report_with_code(text)), "reviewer", "report_check")
            discrepancies = extract_tag(check, "discrepancies").strip()
            record["discrepancies"] = discrepancies
            if discrepancies and discrepancies.lower().rstrip(".") != "none":
                revised = ReportText.parse(complete(prompts.REPORT_REVISE_TEMPLATE.format(
                    discrepancies=discrepancies, **asdict(text)), "writer", "report_revise"))
                revised, revised_ok, revised_errors = self._compile_with_repairs(
                    revised, complete, "report_revise_repair")
                if revised_ok:
                    text = revised
                else:
                    record["problems"].append("revision did not compile; the checked "
                                              "version before revision was kept")
                    ok, errors = self.compile(self.document(text))
        self.save(text)
        record["problems"] += self.problems(text)
        if self.unmapped:
            record["problems"].append("Unicode characters without a LaTeX symbol: "
                                      + " ".join(self.unmapped))
        if not ok:
            record["problems"].append(errors[:2000])
        pdf = self.out_dir / f"{self.slug}.pdf"
        if ok and pdf.exists():
            record["pdf"] = str(pdf)
        return record

    def _compile_with_repairs(self, text: ReportText, complete, phase: str):
        errors = ""
        for repair in range(MAX_COMPILE_REPAIRS + 1):
            problems = self.problems(text)
            if not problems:
                ok, errors = self.compile(self.document(text))
                if ok:
                    return text, True, ""
                # The referee's text is inserted verbatim; typeset it as plain
                # text if it is what fails.
                if self.review_section():
                    plain_ok, _ = self.compile(self.document(text, plain_review=True))
                    if plain_ok:
                        return text, True, ""
                problems = [errors]
            if repair == MAX_COMPILE_REPAIRS:
                return text, False, "\n".join(problems)
            text = ReportText.parse(complete(prompts.REPORT_REPAIR_TEMPLATE.format(
                errors="\n".join(problems)[:6000], **asdict(text)), "writer", f"{phase}{repair}"))
        return text, False, errors


def latex_errors(log_path: Path) -> str:
    """The error lines of a LaTeX log, each with the two lines after it."""
    if not log_path.exists():
        return "no log written"
    lines = log_path.read_text(errors="replace").splitlines()
    errors = [("\n".join(lines[k:k + 3])) for k, line in enumerate(lines) if line.startswith("!")]
    return "\n\n".join(errors[:10]) or "\n".join(lines[-20:])


def missing_glyphs(log_path: Path) -> list[str]:
    """Characters the fonts lack (reported as warnings in the log)."""
    if not log_path.exists():
        return []
    return sorted(set(re.findall(r"Missing character: There is no (\S+)",
                                 log_path.read_text(errors="replace"))))


def rebuild(out_dir: Path) -> None:
    """Recompile a saved report without model calls."""
    parts = json.loads((out_dir / "report_parts.json").read_text())
    builder = ReportBuilder(out_dir, **parts["builder"])
    text = ReportText(**parts["text"])
    print("\n".join(builder.problems(text)) or "contract: ok")
    ok, errors = builder.compile(builder.document(text))
    print("compiled" if ok else errors)
    glyphs = missing_glyphs(out_dir / f"{builder.slug}.log")
    if glyphs:
        print("missing glyphs:", " ".join(glyphs))


if __name__ == "__main__":
    rebuild(Path(sys.argv[1]))
