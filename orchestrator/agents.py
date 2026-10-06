"""Pluggable model backends for the orchestration roles.

Each role (captain, auditor, solver) is served by an agent built from a
specification in `config.json`:

    {"backend": "claude_cli", "model": "claude-sonnet-5"}
    {"backend": "openai_compatible", "model": "qwen3-4b-instruct",
     "base_url": "http://localhost:8000/v1"}
    {"backend": "aiprover", "model": "qwen3-4b-instruct",
     "config": "aiprover/aiprover.toml", "cli": "<AIProver_plugin>/bin/aiprover"}
    {"backend": "python", "class": "my_package.my_module:MyAgent", ...}

`openai_compatible` covers any server that implements the OpenAI chat
completions schema: a local vLLM server, a hosted open-weights endpoint, or a
proprietary model behind a compatible gateway. `aiprover` runs the AIProver
harness (a full agentic Lean session per call; solver role only) with the
model named in its own configuration. `python` loads a user-defined
subclass of `Agent`, so a custom agent can replace any role without changes
to the pipeline.

`AgentPool` routes each call to the agent of its role, retries transient
failures, and records every call in `calls.jsonl` and in the run trace.
"""

import importlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import claude_cli

logger = logging.getLogger(__name__)


@dataclass
class Completion:
    """Result of one model call.

    `usage` maps each model id that served the call to a dict with the keys
    `inputTokens`, `outputTokens` and `costUSD` (the Claude CLI naming, kept
    for all backends so that traces are uniform).
    """
    text: str = ""
    usage: dict[str, dict] = field(default_factory=dict)
    error: str | None = None
    retryable: bool = False
    retry_after_s: float | None = None


class AgentCallError(RuntimeError):
    """A model call that failed after all retries (an infrastructure failure)."""


class Agent:
    """A model endpoint that answers one prompt under one system prompt.

    Subclasses implement `complete_once`; retrying and recording are handled
    by `AgentPool`. Backend-specific options arrive as keyword arguments from
    the role's specification.
    """

    backend = "abstract"

    def __init__(self, model: str, max_retries: int = 5, **options):
        self.model = model
        self.max_retries = max_retries
        self.options = options

    def complete_once(self, prompt: str, system_prompt: str) -> Completion:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"backend": self.backend, "model": self.model, **self.options}


class ClaudeCLIAgent(Agent):
    """Claude model through the local `claude` CLI (subscription auth)."""

    backend = "claude_cli"

    def __init__(self, model: str, effort: str = "",
                 timeout: int = claude_cli.REQUEST_TIMEOUT_S, max_output_tokens: int = 0,
                 **options):
        super().__init__(model, max_retries=claude_cli.MAX_RETRIES, effort=effort,
                         timeout=timeout, max_output_tokens=max_output_tokens, **options)
        self.effort = effort
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens

    def complete_once(self, prompt: str, system_prompt: str) -> Completion:
        text, payload, error = claude_cli.invoke_once(prompt, self.model, system_prompt,
                                                      self.timeout, self.effort,
                                                      self.max_output_tokens)
        usage = {model_id: {"inputTokens": int(u.get("inputTokens") or 0),
                            "outputTokens": int(u.get("outputTokens") or 0),
                            "costUSD": float(u.get("costUSD") or 0.0)}
                 for model_id, u in (payload.get("modelUsage") or {}).items()}
        if error is None:
            return Completion(text=text, usage=usage)
        retryable = not error.startswith("__fatal__") and claude_cli._is_retryable(error)
        return Completion(usage=usage, error=error, retryable=retryable,
                          retry_after_s=claude_cli._usage_limit_wait_seconds(error))


class OpenAICompatibleAgent(Agent):
    """Model behind an OpenAI-compatible `/chat/completions` endpoint.

    Options: `base_url`, `api_key_env` (name of the environment variable that
    holds the key; omitted for a local server), `temperature`, `top_p`,
    `max_tokens`, `timeout`, and `extra_body` (merged into the request, for
    server-specific parameters).
    """

    backend = "openai_compatible"
    RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

    def __init__(self, model: str, base_url: str, api_key_env: str = "",
                 temperature: float = 0.6, top_p: float = 0.95,
                 max_tokens: int = 4096, timeout: int = 900,
                 extra_body: dict | None = None, **options):
        super().__init__(model, base_url=base_url, api_key_env=api_key_env,
                         temperature=temperature, top_p=top_p,
                         max_tokens=max_tokens, timeout=timeout,
                         extra_body=extra_body or {}, **options)
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        self.request_defaults = {"temperature": temperature, "top_p": top_p,
                                 "max_tokens": max_tokens, **(extra_body or {})}
        self.timeout = timeout

    def complete_once(self, prompt: str, system_prompt: str) -> Completion:
        body = {"model": self.model,
                "messages": [{"role": "system", "content": system_prompt},
                             {"role": "user", "content": prompt}],
                **self.request_defaults}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            return Completion(error=f"HTTP {e.code}: {detail}",
                              retryable=e.code in self.RETRYABLE_STATUS)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            return Completion(error=f"connection error: {e}", retryable=True)

        choices = payload.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        usage = payload.get("usage") or {}
        served_model = payload.get("model") or self.model
        completion_usage = {served_model: {
            "inputTokens": int(usage.get("prompt_tokens") or 0),
            "outputTokens": int(usage.get("completion_tokens") or 0),
            "costUSD": 0.0}}
        text = (message.get("content") or "").strip()
        if not text:
            reason = choices[0].get("finish_reason") if choices else "no choices"
            return Completion(usage=completion_usage, error=f"empty reply ({reason})")
        return Completion(text=text, usage=completion_usage)


BACKENDS = {"claude_cli": ClaudeCLIAgent, "openai_compatible": OpenAICompatibleAgent}


def build_agent(spec: dict) -> Agent:
    """Instantiate the agent described by one role specification."""
    options = dict(spec)
    backend = options.pop("backend")
    if backend == "aiprover":
        from .aiprover_agent import AIProverAgent
        return AIProverAgent(**options)
    if backend == "python":
        module_name, class_name = options.pop("class").split(":")
        agent_class = getattr(importlib.import_module(module_name), class_name)
        if not issubclass(agent_class, Agent):
            raise TypeError(f"{agent_class} is not a subclass of orchestrator.agents.Agent")
        return agent_class(**options)
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; expected one of "
                         f"{sorted(BACKENDS) + ['aiprover', 'python']}")
    return BACKENDS[backend](**options)


class AgentPool:
    """Routes calls by role, retries transient failures, and records calls.

    Each call is appended to `calls_path` as one JSON line and, if a trace is
    given, added to it as a `model_call` step.
    """

    BACKOFF_S = [10, 30, 60, 180, 300]

    def __init__(self, agents: dict[str, Agent], calls_path: Path, trace=None,
                 system_prompt_ids: dict[str, str] | None = None,
                 max_claude_calls: int = 0):
        self.agents = agents
        self.max_claude_calls = max_claude_calls
        self.calls_path = Path(calls_path)
        self.calls_path.parent.mkdir(parents=True, exist_ok=True)
        self.trace = trace
        # Maps a system prompt's text to its key in the trace header, so the
        # trace stores each system prompt once.
        self.system_prompt_ids = system_prompt_ids or {}
        self._lock = threading.Lock()
        self.num_calls: dict[str, int] = {}
        self.claude_calls = 0
        self.usage_by_model: dict[str, dict] = {}
        if self.calls_path.exists():
            self._load_history()

    def _load_history(self) -> None:
        """Seed the call and usage totals from an existing calls file (resume)."""
        for line in self.calls_path.read_text().splitlines():
            record = json.loads(line)
            self._accumulate(record["role"], record.get("backend"),
                             record.get("model_usage") or {})

    def _accumulate(self, role: str, backend: str | None, model_usage: dict) -> None:
        self.num_calls[role] = self.num_calls.get(role, 0) + 1
        self.claude_calls += backend == "claude_cli"
        for model_id, usage in model_usage.items():
            totals = self.usage_by_model.setdefault(
                model_id, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
            totals["input_tokens"] += int(usage.get("inputTokens") or 0)
            totals["output_tokens"] += int(usage.get("outputTokens") or 0)
            totals["cost_usd"] += float(usage.get("costUSD") or 0.0)

    def complete(self, prompt: str, *, role: str, system_prompt: str,
                 phase: str, **trace_fields) -> str:
        """Return the reply of the role's agent.

        Raises RuntimeError when the call fails permanently, so that the
        pipeline never proceeds on an empty reply.
        """
        agent = self.agents[role]
        if agent.backend == "claude_cli" and self.max_claude_calls:
            self._check_claude_budget()
        completion = Completion()
        for attempt in range(agent.max_retries + 1):
            start = time.time()
            completion = agent.complete_once(prompt, system_prompt)
            self._record(role, phase, agent, system_prompt, prompt, completion,
                         time.time() - start, trace_fields)
            if completion.error is None:
                return completion.text
            if not completion.retryable or attempt == agent.max_retries:
                break
            wait = completion.retry_after_s
            if wait is None:
                wait = self.BACKOFF_S[min(attempt, len(self.BACKOFF_S) - 1)]
            logger.warning(f"[{role}/{phase}] transient failure, retrying in "
                           f"{wait:.0f}s: {completion.error[:200]}")
            time.sleep(max(wait, 1.0))
        raise AgentCallError(f"{agent.backend} call failed ({role}/{phase}): "
                             f"{completion.error}")

    def claude_budget_left(self) -> int:
        """Calls to claude_cli agents left in the run's budget (large if unlimited)."""
        return self.max_claude_calls - self.claude_calls if self.max_claude_calls else 1 << 30

    def _check_claude_budget(self) -> None:
        # A plain RuntimeError, not AgentCallError: the pipeline reports it as
        # a budget stop, which is not restarted automatically.
        if self.claude_calls >= self.max_claude_calls:
            raise RuntimeError(f"Claude call budget of {self.max_claude_calls} exhausted")

    def record(self, role: str, phase: str, prompt: str, completion: Completion,
               seconds: float, **trace_fields) -> None:
        """Record a call made outside `complete` (e.g. an AIProver job)."""
        self._record(role, phase, self.agents[role], "", prompt, completion, seconds,
                     trace_fields)

    def _record(self, role, phase, agent, system_prompt, prompt, completion,
                seconds, trace_fields):
        cost = sum(u.get("costUSD", 0.0) for u in completion.usage.values())
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "role": role,
            "phase": phase,
            "backend": agent.backend,
            "requested_model": agent.model,
            "model_usage": completion.usage,
            "cost_usd": cost,
            "seconds": round(seconds, 1),
            "error": completion.error,
            "system_prompt": system_prompt,
            "prompt": prompt,
            "response": completion.text,
        }
        with self._lock:
            self._accumulate(role, agent.backend, completion.usage)
            with open(self.calls_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        if self.trace is not None:
            primary_usage = (completion.usage.get(agent.model)
                             or next(iter(completion.usage.values()), {}))
            self.trace.add("model_call", label=phase, role=role, backend=agent.backend,
                           model=agent.model, resolved_models=list(completion.usage),
                           system_prompt=self.system_prompt_ids.get(system_prompt,
                                                                    system_prompt),
                           prompt=prompt, response=completion.text,
                           error=completion.error, seconds=record["seconds"],
                           cost_usd=cost, usage=primary_usage, **trace_fields)


def probe(agents: dict[str, Agent]) -> None:
    """Send one short prompt to each role's agent and print the result."""
    for role, agent in agents.items():
        completion = agent.complete_once("Reply with the single word OK.", "You are terse.")
        print(f"{role}: {agent.backend}/{agent.model} reply={completion.text!r} "
              f"error={completion.error} served_by={list(completion.usage)}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Probe every role's agent.")
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).parent / "config.json")
    config = json.loads(parser.parse_args().config.read_text())
    probe({role: build_agent(spec) for role, spec in config["agents"].items()})
