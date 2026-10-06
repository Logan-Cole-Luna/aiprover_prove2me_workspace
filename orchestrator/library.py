"""Libraries of verified results that runs build on.

A library is a Lean file that compiles on its own: definitions and theorems
with complete proofs (no `sorry`). A run given a library formalizes only its
new statement against the library's definitions (it adds definitions only
where the library lacks them), and its solvers may use the library's
theorems. The library text is the prefix of the run's definitions, so every
file the run checks, and its final solution, compiles without the library
being installed anywhere.

A verified run extends its library with what it added: its new definitions,
its lemmas, and its main proof under the target theorem's name in place of
`solution`. The extension is appended under a file lock and kept only if the
extended library compiles without `sorry`.

    python3 -m orchestrator.library add libraries/<name>.lean results/<run_id>
"""

import fcntl
import json
import re
import sys
from pathlib import Path

from .aiprover_agent import split_declarations
from .lean_check import LeanChecker
from .structures import drop_imports

ROOT = Path(__file__).resolve().parent.parent
FILE_HEADER = "import Mathlib\nset_option autoImplicit false\n\n"


def resolve(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else ROOT / candidate


def load(path: str) -> str:
    """The library's Lean text, without `import` lines."""
    return drop_imports(resolve(path).read_text()).strip()


def declared_names(library: str) -> set[str]:
    return {name for _, name, _ in split_declarations(library)}


def contribution(solution: str, library_chars: int, theorem_name: str) -> str:
    """What a verified solution adds to the library it was built on: the text
    after the library prefix, with `solution` named after the target theorem."""
    body = solution[len(FILE_HEADER):] if solution.startswith(FILE_HEADER) else solution
    added = body[library_chars:].strip()
    return re.sub(r"^theorem solution\b", f"theorem {theorem_name}", added, flags=re.M)


def add(path: str, result_dir: Path) -> dict:
    """Append the verified result of `result_dir` to the library at `path`."""
    result_dir = Path(result_dir)
    summary = json.loads((result_dir / "summary.json").read_text())
    if not (summary.get("checks") or {}).get("verified"):
        return {"added": False, "declarations": [], "problem": "run is not verified"}
    solution = next(result_dir.glob("*_standalone.lean")).read_text()
    return extend(path, solution, (summary.get("library") or {}).get("chars", 0),
                  summary["formalization"]["theorem_name"],
                  f"{summary['run_id']} ({summary['uuid']}), {summary.get('status')}")


def extend(path: str, solution: str, library_chars: int, theorem_name: str,
           source: str) -> dict:
    """Append what `solution` adds to the library at `path`.

    Returns {"added": bool, "declarations": [...], "problem": str}. The
    library file is unchanged unless the extended file compiles cleanly.
    """
    added = contribution(solution, library_chars, theorem_name)
    library_path = resolve(path)
    checker = LeanChecker(ROOT / "prove2me_workspace", 1, 600)
    with open(library_path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = library_path.read_text() if library_path.exists() else FILE_HEADER
        if re.search(rf"^theorem {re.escape(theorem_name)}\b", current, flags=re.M):
            return {"added": False, "declarations": [],
                    "problem": f"{theorem_name} is already in the library"}
        extended = f"{current.rstrip()}\n\n-- From run {source}.\n\n{added}\n"
        check_path = ROOT / "temp" / "library_checks" / f"{theorem_name}.lean"
        check_path.parent.mkdir(parents=True, exist_ok=True)
        result = checker.check_text(extended, check_path)
        if not result.ok or result.has_sorry_warning:
            problem = result.error_report()[:2000] if not result.ok else "extension contains sorry"
            return {"added": False, "declarations": [], "problem": problem}
        library_path.write_text(extended)
        check_path.unlink(missing_ok=True)
    return {"added": True, "problem": "",
            "declarations": [name for _, name, _ in split_declarations(added)]}


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] != "add":
        raise SystemExit(__doc__.strip().splitlines()[-1].strip())
    record = add(sys.argv[2], Path(sys.argv[3]))
    print(json.dumps(record, indent=1))


if __name__ == "__main__":
    main()
