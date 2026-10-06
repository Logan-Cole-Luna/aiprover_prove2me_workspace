"""AIProver harness as a solver backend.

An AIProver call is a complete agentic Lean session: the evolved hevo harness
(`PartitionAndProve/AIProver_plugin`) drives a model through lean-lsp tools for
up to `max_turns` turns and writes a Lean file. The model is whatever
OpenAI-compatible endpoint the AIProver configuration names (`[endpoint]` of
`aiprover.toml`), so the same backend serves a small local model now and a
large model later.

The pipeline gives each lemma to one AIProver job with `samples` independent
rollouts; this module submits the job through the plugin's CLI, waits for it,
and extracts the lemma's proof and any helper declarations from each sample.
"""

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agents import Agent
from .lean_check import strip_comments

ROOT = Path(__file__).resolve().parent.parent

# A top-level declaration header, optionally preceded by attributes and modifiers.
_DECL_START = re.compile(
    r"^(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|nonrec)\s+)*"
    r"(theorem|lemma|def|abbrev|instance|example|inductive|structure)\b\s*([^\s:({\[]*)",
    re.M)
# Lines that end a top-level declaration without starting a new one.
_BLOCK_END = re.compile(r"^(?:namespace|section|end|open|set_option|#|import|variable)\b", re.M)


@dataclass
class AIProverSample:
    index: int
    status: str                    # verified | sorry | rejected | error | empty | infra | ...
    lean: str = ""
    turns: int | None = None
    tool_calls: int | None = None
    elapsed_sec: float | None = None
    problems: list[str] = field(default_factory=list)
    ending: str = ""               # why the session stopped; see session_ending()
    check: dict = field(default_factory=dict)  # harness's Lean check of the final file
    session: list = field(default_factory=list)    # transcript; see read_session()
    reasoning: list = field(default_factory=list)  # model reasoning per turn; see read_reasoning()


def session_ending(sample: dict) -> str:
    """Why an AIProver session stopped, from its entry in `aiprover result --json`.

    "server lost" and "server error" are infrastructure failures; "timeout" is
    the job's wall clock, "turn limit" its max_turns; "finished" means the agent
    stopped on its own.
    """
    infra = sample.get("infra") or ""
    if infra:
        return "server lost" if "unreachable" in infra else "server error"
    if sample.get("timed_out"):
        return "timeout"
    if "turn limit" in (sample.get("stop_reason") or "").lower():
        return "turn limit"
    if sample.get("agent_error"):
        return "agent error"
    return "finished"


# Diagnostics beyond this length are cut from the trace record.
MAX_DIAGNOSTICS = 20000
# Transcript entries are cut to these lengths in the trace record.
MAX_MESSAGE_TEXT = 8000
MAX_TOOL_TEXT = 6000
# A failed sample as shown to the captain at a replan.
MAX_ATTEMPT_TEXT = 6000
MAX_ATTEMPT_ERRORS = 2000


def sample_dir(lean_file: str | None, index) -> Path | None:
    """The harness's directory of sample `index`, next to its `s<k>.lean`."""
    if not lean_file or index is None:
        return None
    return Path(lean_file).parent / f"s{index}"


def read_session(directory: Path | None) -> list[dict]:
    """The agent's transcript from vibe's session log: each message, tool call
    and tool result, in order, shortened for the trace."""
    logs = sorted((directory / ".vibe" / "logs" / "session").glob("session_*/messages.jsonl")) \
        if directory else []
    transcript = []
    for log in logs:
        for line in log.read_text(errors="replace").splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = message.get("role")
            if role == "tool":
                transcript.append({"role": "tool", "name": message.get("name"),
                                   "text": str(message.get("content") or "")[:MAX_TOOL_TEXT]})
                continue
            entry = {"role": role, "text": str(message.get("content") or "")[:MAX_MESSAGE_TEXT]}
            calls = [{"name": (call.get("function") or {}).get("name"),
                      "arguments": str((call.get("function") or {}).get("arguments") or "")[:MAX_TOOL_TEXT]}
                     for call in message.get("tool_calls") or []]
            if calls:
                entry["tool_calls"] = calls
            if message.get("injected"):
                entry["injected"] = True
            transcript.append(entry)
    return transcript


def read_reasoning(directory: Path | None) -> list[dict]:
    """The model's reasoning per turn, as recorded by server/reasoning_proxy.py
    (`reasoning.jsonl` in the sample directory); empty if it was not recorded."""
    path = directory / "reasoning.jsonl" if directory else None
    if not path or not path.exists():
        return []
    records = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def lean_check_summary(check: dict | None) -> dict:
    """The harness's Lean check of a sample's final file, as kept in the trace."""
    check = check or {}
    return {"verdict": check.get("verdict"), "compiles": check.get("compiles"),
            "complete": check.get("complete"), "problems": check.get("problems") or [],
            "warnings": check.get("warnings") or [], "axioms_used": check.get("axioms_used"),
            "diagnostics": (check.get("diagnostics") or "")[:MAX_DIAGNOSTICS]}


@dataclass
class AIProverJob:
    job: str
    problem: str
    samples: list[AIProverSample]
    seconds: float
    error: str | None = None


class AIProverAgent(Agent):
    """Solver backed by AIProver jobs.

    Options: `config` (path of the aiprover.toml, relative to the
    orchestration root), `cli` (path of the plugin's `bin/aiprover`),
    `timeout` (per-rollout seconds), `max_turns`, `poll_seconds`, and `env`
    (extra environment for the harness, e.g. `AGENT_THINKING`).
    """

    backend = "aiprover"

    def __init__(self, model: str, config: str, cli: str, timeout: int = 1800,
                 max_turns: int = 100, poll_seconds: int = 60, env: dict | None = None,
                 **options):
        super().__init__(model, max_retries=0, config=config, cli=cli, timeout=timeout,
                         max_turns=max_turns, env=env or {}, **options)
        self.config_path = (ROOT / config).resolve()
        self.cli = (ROOT / cli).resolve()
        self.timeout = timeout
        self.max_turns = max_turns
        self.poll_seconds = poll_seconds
        self.env = {**os.environ, "AIPROVER_CONFIG": str(self.config_path),
                    **{key: str(value) for key, value in (env or {}).items()}}

    def complete_once(self, prompt: str, system_prompt: str):
        raise NotImplementedError("AIProverAgent serves the solver role through solve()")

    def _cli(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.cli), *args], env=self.env, capture_output=True,
                              text=True, timeout=timeout)

    def endpoint_up(self) -> bool:
        """True if the model endpoint answers (through the tunnel)."""
        try:
            return self._cli("tunnel", "status", timeout=60).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def solve(self, *, name: str, theorem_text: str, proof_text: str, context: str,
              statement: str, samples: int, work_dir: Path) -> AIProverJob:
        """Run one AIProver job and return its samples (blocks until done)."""
        work_dir.mkdir(parents=True, exist_ok=True)
        context_path = work_dir / f"{name}_context.lean"
        statement_path = work_dir / f"{name}_statement.lean"
        context_path.write_text(context)
        statement_path.write_text(statement)
        start = time.time()
        args = ["--theorem-text", theorem_text, "--proof-text", proof_text,
                "--context", str(context_path), "--lean-statement", str(statement_path)]
        rendered = self._cli("render", *args, timeout=60).stdout
        submitted = self._cli("submit", "-q", "-k", str(samples), "--name", name,
                              "--timeout", str(self.timeout),
                              "--max-turns", str(self.max_turns), *args, timeout=120)
        job = submitted.stdout.strip().splitlines()[-1] if submitted.stdout.strip() else ""
        if submitted.returncode != 0 or not job:
            return AIProverJob(job, rendered, [], time.time() - start,
                               error=(submitted.stderr or submitted.stdout)[-1000:])
        try:
            while self._cli("wait", job, "--timeout", str(self.poll_seconds),
                            timeout=self.poll_seconds + 60).returncode == 3:
                pass
        except BaseException:
            self._cli("cancel", job, timeout=60)
            raise
        result = self._cli("result", job, "--json", timeout=120)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return AIProverJob(job, rendered, [], time.time() - start,
                               error=(result.stderr or result.stdout)[-1000:])
        job_samples = []
        for sample in payload.get("samples", []):
            lean_file = sample.get("lean_file")
            lean = Path(lean_file).read_text(errors="replace") if lean_file else ""
            job_samples.append(AIProverSample(
                index=sample.get("sample", len(job_samples)),
                status=sample.get("status", "unknown"), lean=lean,
                turns=sample.get("turns"), tool_calls=sample.get("tool_calls"),
                elapsed_sec=sample.get("elapsed_sec"), ending=session_ending(sample),
                check=lean_check_summary(sample.get("check")),
                session=read_session(sample_dir(lean_file, sample.get("sample"))),
                reasoning=read_reasoning(sample_dir(lean_file, sample.get("sample"))),
                problems=list((sample.get("check") or {}).get("problems") or [])))
        return AIProverJob(job, rendered, job_samples, time.time() - start)


def split_declarations(lean_text: str) -> list[tuple[str, str, str]]:
    """Top-level declarations of a Lean file as (kind, name, text)."""
    code = strip_comments(lean_text)
    starts = list(_DECL_START.finditer(code))
    blocks = []
    for k, match in enumerate(starts):
        end = starts[k + 1].start() if k + 1 < len(starts) else len(code)
        terminator = _BLOCK_END.search(code, match.end(), end)
        if terminator:
            end = terminator.start()
        blocks.append((match.group(1), match.group(2), code[match.start():end].rstrip()))
    return blocks


def opened_namespaces(lean_text: str) -> list[str]:
    """Namespaces the answer opens with top-level `open` commands."""
    return sorted({name for line in re.findall(r"^open\s+([^\n]+)$", strip_comments(lean_text), re.M)
                   for name in line.removesuffix(" in").split()})


def answer_helpers(blocks: list[tuple[str, str, str]], lemma_name: str,
                   fixed_names: set[str], opened: list[str]) -> list[str]:
    """The answer's own declarations other than the lemma and the fixed
    context, each under the answer's `open` commands, which they may rely on."""
    prefix = f"open {' '.join(opened)} in\n" if opened else ""
    return [prefix + text for kind, name, text in blocks
            if name != lemma_name and name not in fixed_names and kind != "example"]


def wrapped_lemma_proof(lean_text: str, lemma_name: str, fixed_names: set[str],
                        alias: str) -> tuple[str, str] | None:
    """(helpers, tactic proof) of `lemma_name` that keeps the answer's own theorem.

    The answer's theorem is kept whole as the helper `alias`, under the
    answer's `open` commands, and the lemma is proved from it by `apply`. This
    recovers proofs that use the answer's binder names or opened namespaces,
    which do not hold under the sketch's statement; Lean still checks the
    sketch's statement.
    """
    blocks = split_declarations(lean_text)
    target = next((text for kind, name, text in blocks
                   if kind in ("theorem", "lemma") and name == lemma_name), None)
    if target is None or ":=" not in target:
        return None
    renamed = re.sub(rf"((?:theorem|lemma)\s+){re.escape(lemma_name)}(?![\w'.])", rf"\g<1>{alias}",
                     target, count=1)
    opened = opened_namespaces(lean_text)
    helpers = answer_helpers(blocks, lemma_name, fixed_names, opened)
    helpers.append((f"open {' '.join(opened)} in\n" if opened else "") + renamed)
    proof = f"first\n  | exact {alias}\n  | (intros; apply {alias} <;> assumption)"
    return "\n\n".join(helpers), proof


def extract_lemma_proof(lean_text: str, lemma_name: str,
                        fixed_names: set[str]) -> tuple[str, str] | None:
    """(helpers, tactic proof) of `lemma_name` from an AIProver answer.

    Helpers are the answer's own declarations other than the lemma and the
    fixed context. A term-mode proof is returned as an `exact` tactic.
    """
    blocks = split_declarations(lean_text)
    target = next((text for kind, name, text in blocks
                   if kind in ("theorem", "lemma") and name == lemma_name), None)
    if target is None or ":=" not in target:
        return None
    body = target.split(":=", 1)[1]
    by_match = re.match(r"\s*by\b", body)
    if by_match:
        proof = body[by_match.end():].strip("\n")
    else:
        proof = "exact (" + body.strip() + ")"
    helpers = answer_helpers(blocks, lemma_name, fixed_names, opened_namespaces(lean_text))
    return "\n\n".join(helpers), proof


def failed_attempt(sample: "AIProverSample", lemma_name: str,
                   fixed_names: set[str]) -> tuple[tuple[int, int, int], str] | None:
    """A failed sample's own Lean code and Lean's response, for a replan.

    The code is the sample's declarations outside the fixed context: its
    helpers and its version of the lemma. Returns (rank, text), where a lower
    rank is a more informative attempt: some proof written, then fewer Lean
    errors, then fewer `sorry`. A sample that left only `sorry`, without a
    helper or a comment, gives None.
    """
    # Line ranges of the sample's own declarations; comments are kept, since
    # they often hold the plan of an unfinished proof. Line structure is the
    # same in the comment-stripped text that split_declarations reads.
    code_only = strip_comments(sample.lean)
    lines = sample.lean.splitlines()
    ranges, bare = [], True
    for kind, name, text in split_declarations(sample.lean):
        if name in fixed_names and name != lemma_name:
            continue
        if name != lemma_name or re.sub(r"\s+", " ", text.split(":=", 1)[-1]).strip() not in ("by sorry", "sorry"):
            bare = False
        first = code_only.count("\n", 0, code_only.find(text)) + 1
        ranges.append((first, first + text.count("\n")))
    code = "\n\n".join("\n".join(lines[first - 1:last]) for first, last in ranges).strip()
    if not code or (bare and "--" not in code and "/-" not in code):
        return None
    entries = re.split(r"\n(?=\S+\.lean:\d+:\d+: )", (sample.check or {}).get("diagnostics") or "")
    errors = [entry for entry in entries
              if (match := re.match(r"\S+\.lean:(\d+):\d+: error", entry))
              and any(first <= int(match.group(1)) <= last for first, last in ranges)]
    holes = len(re.findall(r"\b(?:sorry|admit)\b", code))
    if len(code) > MAX_ATTEMPT_TEXT:
        code = code[:MAX_ATTEMPT_TEXT] + "\n-- (truncated)"
    response = "\n".join(errors)[:MAX_ATTEMPT_ERRORS] or "(no errors)"
    # The harness's status is left out: it also counts the `sorry` of the
    # earlier lemmas, which are stubs in the sample's file.
    text = (f"AIProver sample {sample.index} (session {sample.ending or 'finished'}; "
            f"{len(errors)} Lean errors and {holes} sorry in this code)\n"
            f"<lean>\n{code}\n</lean>\nLean errors:\n{response}")
    return (int(bare), len(errors), holes), text
