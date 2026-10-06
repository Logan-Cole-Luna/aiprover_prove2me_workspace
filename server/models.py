"""Models a run may use for each of its roles.

A selection is `{"provider": "claude" | "aiprover", "model": ..., "effort": ...}`.
For Claude, `model` is a model id and `effort` a reasoning level the model
accepts (empty = the CLI default). For AIProver, `model` is a version, whose
checkpoint the Vista model server must serve, and `effort` is empty: the
reasoning of the model is not configurable.

Roles (pipeline name in parentheses): the orchestrator (captain) formalizes,
sketches and repairs; the auditor judges the formalization against the
informal statement; the reviewer, an independent model, judges the finished
proof; the writer writes the LaTeX report; the subagent (solver) proves the
lemmas. A role absent from a stored selection keeps the served config's model.
"""

import json
from pathlib import Path

from .vista import CHECKPOINTS

EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_MODELS = {
    "claude-opus-5-5": {"label": "Opus 5.5", "efforts": EFFORTS},
    "claude-sonnet-5-5": {"label": "Sonnet 5.5", "efforts": EFFORTS},
    "claude-haiku-4-5-20251001": {"label": "Haiku 4.5", "efforts": ()},
}
AIPROVER_VERSIONS = {"trained": "AIProver trained (FP8)", "base": "AIProver base (bf16)"}

DEFAULT_SELECTION = {
    "orchestrator": {"provider": "claude", "model": "claude-opus-5-5", "effort": "high"},
    "auditor": {"provider": "claude", "model": "claude-sonnet-5-5", "effort": ""},
    "reviewer": {"provider": "claude", "model": "claude-opus-5-5", "effort": "high"},
    "writer": {"provider": "claude", "model": "claude-opus-5-5", "effort": "high"},
    "subagent": {"provider": "aiprover", "model": "trained", "effort": ""},
}
ROLES = {"orchestrator": "captain", "auditor": "auditor", "reviewer": "reviewer",
         "writer": "writer", "subagent": "solver"}
ROLE_DESCRIPTIONS = {
    "orchestrator": "Formalizes, sketches the proof and repairs",
    "auditor": "Judges the formalization against the statement",
    "reviewer": "Independently judges the finished proof",
    "writer": "Writes the LaTeX report",
    "subagent": "Proves the lemmas",
}

# Chat roles reach AIProver through the reasoning proxy, as the
# solver's harness does (aiprover/aiprover_vista_logged.toml).
AIPROVER_ENDPOINT = "http://127.0.0.1:18565/v1"
AIPROVER_SERVED_MODEL = "aiprover"
CHAT_REPLY_TOKENS = 24576
# Claude calls per run: the served config limits them for a Claude orchestrator
# and auditor; a Claude subagent makes one call per lemma attempt as well.
CLAUDE_SUBAGENT_CALL_LIMIT = 40
SUBAGENT_TIMEOUT = 5400


def catalog() -> dict:
    """What the page offers, for `GET /api/models`."""
    return {"claude": [{"model": model, "label": info["label"], "efforts": list(info["efforts"])}
                       for model, info in CLAUDE_MODELS.items()],
            "aiprover": [{"model": version, "label": label}
                         for version, label in AIPROVER_VERSIONS.items()],
            "roles": [{"role": role, "label": role.capitalize(),
                       "description": ROLE_DESCRIPTIONS[role]} for role in ROLES],
            "default": DEFAULT_SELECTION}


def validate(selection: dict) -> dict:
    """The normalized selection of both roles; ValueError names the problem."""
    normalized = {}
    for role in ROLES:
        choice = selection.get(role) or DEFAULT_SELECTION[role]
        provider, model, effort = (choice.get(key) or "" for key in ("provider", "model", "effort"))
        if provider == "claude":
            if model not in CLAUDE_MODELS:
                raise ValueError(f"{role}: unknown Claude model {model!r}")
            efforts = CLAUDE_MODELS[model]["efforts"]
            if effort and effort not in efforts:
                raise ValueError(f"{role}: {CLAUDE_MODELS[model]['label']} has no "
                                 f"reasoning level {effort!r}")
        elif provider == "aiprover":
            if model not in AIPROVER_VERSIONS:
                raise ValueError(f"{role}: unknown AIProver version {model!r}")
            effort = ""
        else:
            raise ValueError(f"{role}: unknown provider {provider!r}")
        normalized[role] = {"provider": provider, "model": model, "effort": effort}
    versions = checkpoint_names(normalized)
    if len(versions) > 1:
        raise ValueError("roles that use AIProver must use the same version: "
                         "the model server serves one checkpoint")
    return normalized


def checkpoint_names(selection: dict) -> set[str]:
    """AIProver versions the selection uses."""
    return {choice["model"] for choice in selection.values() if choice["provider"] == "aiprover"}


def checkpoint_of(selection: dict) -> str | None:
    """Path of the checkpoint the run needs the model server to serve, if any."""
    names = checkpoint_names(selection)
    return CHECKPOINTS[next(iter(names))] if names else None


def build_config(base_config: Path, selection: dict) -> dict:
    """The served config with the agents of `selection` (already validated)."""
    config = json.loads(base_config.read_text())
    agents = config["agents"]
    for role, choice in selection.items():
        # The pipeline gives a reviewer or writer absent from the config the
        # captain's specification.
        spec = agents.get(ROLES[role]) or agents["captain"]
        if choice["provider"] == "claude":
            spec = {key: value for key, value in spec.items()
                    if key in ("timeout", "max_output_tokens")}
            spec |= {"backend": "claude_cli", "model": choice["model"]}
            if choice["effort"]:
                spec["effort"] = choice["effort"]
            if role == "subagent":
                spec["timeout"] = SUBAGENT_TIMEOUT
        elif role != "subagent":
            spec = {"backend": "openai_compatible", "model": AIPROVER_SERVED_MODEL,
                    "base_url": AIPROVER_ENDPOINT, "timeout": spec.get("timeout", 2400),
                    "max_tokens": CHAT_REPLY_TOKENS}
        else:
            continue  # The served config's solver is the AIProver harness.
        agents[ROLES[role]] = spec
    if selection.get("subagent", {}).get("provider") == "claude":
        config["max_claude_calls"] = CLAUDE_SUBAGENT_CALL_LIMIT
    return config
