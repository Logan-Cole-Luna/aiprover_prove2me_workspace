"""Data structures of a run and parsing of model-written Lean text."""

import re
from dataclasses import dataclass


@dataclass
class Formalization:
    definitions: str
    preamble: str                  # `open` lines preceding the statement
    theorem_name: str
    signature: str                 # binders and type, from after the name to `:=`
    notes: str = ""
    readback: str = ""
    verdict: str = ""
    issues: str = ""

    @property
    def statement(self) -> str:
        return f"theorem {self.theorem_name}{self.signature}:= by sorry"


@dataclass
class Lemma:
    name: str
    statement: str                 # `theorem name <binders> : <type>`
    helpers: str = ""
    proof: str = ""
    proved: bool = False
    last_errors: str = ""


@dataclass
class Sketch:
    lemmas: list[Lemma]
    main_proof: str


# ── Parsing ────────────────────────────────────────────────────────────────

def extract_tag(text: str, tag: str) -> str:
    """Content of the last <tag>...</tag> block, with Markdown fences removed."""
    matches = re.findall(rf"<{tag}>(.*?)</{tag}>", text, flags=re.S)
    if not matches:
        return ""
    body = matches[-1].strip()
    body = re.sub(r"^```[a-zA-Z0-9]*\s*\n", "", body)
    body = re.sub(r"\n?```\s*$", "", body)
    return body.strip()


def strip_leading_by(tactics: str) -> str:
    """Remove a leading `by`: tactic blocks are spliced after `:= by`."""
    return re.sub(r"^\s*by\b[ \t]*\n?", "", tactics)


def drop_imports(lean_text: str) -> str:
    return "\n".join(line for line in lean_text.splitlines()
                     if not line.lstrip().startswith("import "))


_DECL_RE = re.compile(r"^(?:@\[[^\]]*\]\s*)?(?:theorem|lemma)\s+([^\s:({\[]+)(.*?):=\s*(?:by\s+)?sorry\b",
                      flags=re.S | re.M)


def parse_statement(block: str) -> tuple[str, str, str]:
    """Split a statement block into (preamble, theorem name, signature)."""
    matches = list(_DECL_RE.finditer(block))
    if len(matches) != 1:
        raise ValueError("the statement block must contain exactly one "
                         "`theorem <name> ... := by sorry` declaration")
    match = matches[0]
    preamble = block[:match.start()].strip()
    return preamble, match.group(1), match.group(2)


def parse_lemmas(block: str) -> list[Lemma]:
    lemmas = []
    for match in _DECL_RE.finditer(block):
        name, signature = match.group(1), match.group(2)
        lemmas.append(Lemma(name=name, statement=f"theorem {name}{signature.rstrip()}"))
    return lemmas


def normalize(text: str) -> str:
    return " ".join(text.split())
