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
                elapsed_sec=sample.get("elapsed_sec"),
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
    helpers = [text for kind, name, text in blocks
               if name != lemma_name and name not in fixed_names and kind != "example"]
    return "\n\n".join(helpers), proof
