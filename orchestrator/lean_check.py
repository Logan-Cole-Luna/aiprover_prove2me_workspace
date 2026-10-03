"""Local Lean 4 verification against the prove2me workspace environment.

Files are elaborated with `lake env lean <file>` from the workspace root, which
resolves Mathlib from the workspace's pinned `.lake` build without taking the
Lake build lock, so several checks can run concurrently.
"""

import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

STANDARD_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}

# Constructs that could discharge or bypass a goal without a proof. They are
# rejected in any model-written Lean text before it is elaborated.
FORBIDDEN_PATTERNS = {
    "sorry": r"\bsorry\b",
    "admit": r"\badmit\b",
    "axiom": r"^\s*(private\s+|protected\s+)?axiom\b",
    "opaque": r"^\s*(private\s+|protected\s+)?opaque\b",
    "implemented_by": r"implemented_by",
    "extern": r"@\[\s*extern",
    "native_decide": r"\bnative_decide\b",
    "macro/syntax/elab": r"^\s*(local\s+|scoped\s+)?(macro|macro_rules|syntax|elab|elab_rules)\b",
    "run_cmd": r"\b(run_cmd|run_tac|run_elab)\b",
    "debug.skipKernelTC": r"debug\.skipKernelTC",
}

_MESSAGE_RE = re.compile(r"^(?P<file>[^\n]*?):(?P<line>\d+):(?P<col>\d+): "
                         r"(?P<severity>error|warning|info)(?:\([^)]*\))?:", re.M)
_AXIOMS_RE = re.compile(r"'(?P<name>[^']+)' depends on axioms: \[(?P<axioms>[^\]]*)\]")
_NO_AXIOMS_RE = re.compile(r"'(?P<name>[^']+)' does not depend on any axioms")


@dataclass
class CheckResult:
    ok: bool                      # elaborated with no errors
    output: str                   # raw Lean output
    errors: list[str] = field(default_factory=list)
    has_sorry_warning: bool = False
    axioms: dict[str, list[str]] = field(default_factory=dict)
    timed_out: bool = False

    def error_report(self, max_chars: int = 4000) -> str:
        """Errors in a compact form for feeding back to a model."""
        text = "\n\n".join(self.errors) if self.errors else self.output
        return text[:max_chars]


def strip_comments(lean_text: str) -> str:
    """Remove Lean line and (nested) block comments, keeping line structure."""
    out, depth, i = [], 0, 0
    while i < len(lean_text):
        two = lean_text[i:i + 2]
        if two == "/-":
            depth += 1
            i += 2
        elif two == "-/" and depth:
            depth -= 1
            i += 2
        elif depth:
            out.append("\n" if lean_text[i] == "\n" else "")
            i += 1
        elif two == "--":
            end = lean_text.find("\n", i)
            i = len(lean_text) if end == -1 else end
        else:
            out.append(lean_text[i])
            i += 1
    return "".join(out)


def forbidden_constructs(lean_text: str) -> list[str]:
    """Names of forbidden constructs present in `lean_text` outside comments."""
    code = strip_comments(lean_text)
    return [name for name, pattern in FORBIDDEN_PATTERNS.items()
            if re.search(pattern, code, flags=re.M)]


def _split_messages(output: str) -> list[tuple[str, str]]:
    """Split Lean output into (severity, message) pairs."""
    matches = list(_MESSAGE_RE.finditer(output))
    messages = []
    for k, match in enumerate(matches):
        end = matches[k + 1].start() if k + 1 < len(matches) else len(output)
        messages.append((match.group("severity"), output[match.start():end].strip()))
    return messages


class LeanChecker:
    """Elaborates standalone Lean files in the workspace environment."""

    def __init__(self, workspace: Path, max_parallel: int = 4, timeout: int = 300):
        self.workspace = Path(workspace)
        self.timeout = timeout
        self._slots = threading.Semaphore(max_parallel)

    def check_file(self, path: Path) -> CheckResult:
        with self._slots:
            try:
                proc = subprocess.run(["lake", "env", "lean", str(Path(path).resolve())],
                                      cwd=self.workspace, capture_output=True,
                                      text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                return CheckResult(ok=False, output=f"Lean timed out after {self.timeout}s",
                                   errors=[f"Lean timed out after {self.timeout}s"],
                                   timed_out=True)
        output = (proc.stdout + proc.stderr).strip()
        messages = _split_messages(output)
        errors = [text for severity, text in messages if severity == "error"]
        if proc.returncode != 0 and not errors:
            errors = [output or f"lean exited with code {proc.returncode}"]
        axioms = {m.group("name"): [a.strip() for a in m.group("axioms").split(",") if a.strip()]
                  for m in _AXIOMS_RE.finditer(output)}
        axioms.update({m.group("name"): [] for m in _NO_AXIOMS_RE.finditer(output)})
        return CheckResult(ok=not errors, output=output, errors=errors,
                           has_sorry_warning="declaration uses 'sorry'" in output,
                           axioms=axioms)

    def check_text(self, lean_text: str, path: Path) -> CheckResult:
        """Write `lean_text` to `path` and elaborate it."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(lean_text)
        return self.check_file(path)


def nonstandard_axioms(result: CheckResult, declaration: str) -> list[str] | None:
    """Axioms of `declaration` outside the standard three (None if not reported)."""
    if declaration not in result.axioms:
        return None
    return [a for a in result.axioms[declaration] if a not in STANDARD_AXIOMS]
