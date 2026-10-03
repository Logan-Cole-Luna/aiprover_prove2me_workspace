"""Claude models through the local `claude` CLI (subscription auth).

Calls are billed against the Claude account session held by the CLI
(`claude login` / `claude setup-token`), not a per-token API key. Adapted from
MixtureOfMathExperts/scripts/utils/generators.py (`_invoke_claude_cli_once`,
`_run_claude_cli`), with the system prompt passed per call so that each
orchestration role carries its own instructions.

Every call runs with tools disabled and with setting sources and MCP servers
cleared, in a working directory outside the workspace, so that no CLAUDE.md or
project configuration leaks into the model context.
"""

import argparse
import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 900
MAX_RETRIES = 5
RETRY_BACKOFF_S = [30, 60, 180, 300, 600]
# A usage limit (as opposed to a short rate limit) reports a reset time that
# can be hours away, so a single call sleeps at most this long.
MAX_USAGE_LIMIT_WAIT_S = 3600
RETRYABLE_MARKERS = (
    "rate limit", "rate_limit", "usage limit", "429", "overloaded",
    "server error", "500", "502", "503", "529", "timeout", "timed out",
    "connection error", "econnreset", "network", "temporarily unavailable",
)

# Neutral working directory for CLI subprocesses (no CLAUDE.md above it).
CLI_WORKDIR = Path(tempfile.gettempdir()) / "orchestrator_cli_cwd"


def get_account_status(timeout: int = 30) -> dict | None:
    """Return `claude auth status --json` if logged in, otherwise None."""
    try:
        proc = subprocess.run(["claude", "auth", "status", "--json"],
                              capture_output=True, text=True, timeout=timeout)
        status = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read `claude auth status`: {e}")
        return None
    return status if status.get("loggedIn") else None


def _usage_limit_wait_seconds(text: str) -> float | None:
    """Seconds until the reset time named in a usage-limit message, if any."""
    match = re.search(r"usage limit reached\|(\d{10,13})", text, flags=re.IGNORECASE)
    if not match:
        return None
    raw = int(match.group(1))
    reset_epoch = raw / 1000.0 if raw > 10_000_000_000 else float(raw)
    return max(0.0, min(reset_epoch - time.time(), MAX_USAGE_LIMIT_WAIT_S))


def _is_retryable(error_text: str) -> bool:
    low = (error_text or "").lower()
    return any(marker in low for marker in RETRYABLE_MARKERS)


def invoke_once(prompt: str, model: str, system_prompt: str,
                timeout: int = REQUEST_TIMEOUT_S, effort: str = "",
                ) -> tuple[str, dict, str | None]:
    """One `claude -p` call. Returns (text, payload, error); error is None on success.

    The CLI reports errors as JSON on stdout while exiting non-zero, so stdout
    is parsed regardless of the return code.
    """
    cmd = ["claude", "-p",
           "--model", model,
           "--output-format", "json",
           "--tools", "",
           "--no-session-persistence",
           "--setting-sources", "",
           "--strict-mcp-config",
           "--system-prompt", system_prompt]
    if effort and effort != "default":
        cmd += ["--effort", effort]
    CLI_WORKDIR.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=timeout, cwd=CLI_WORKDIR)
    except subprocess.TimeoutExpired:
        return "", {}, f"claude CLI timed out after {timeout}s"
    except FileNotFoundError:
        return "", {}, "__fatal__ `claude` CLI not found on PATH"

    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        payload = None
    if payload is None:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "no output")[:500]
        return "", {}, f"exit {proc.returncode}, unparseable output: {detail}"
    if proc.returncode != 0 or payload.get("is_error"):
        detail = str(payload.get("result") or payload.get("error")
                     or proc.stderr.strip() or "unknown")[:500]
        status = payload.get("api_error_status")
        return "", payload, f"exit {proc.returncode} (api_status={status}): {detail}"
    return (payload.get("result") or "").strip(), payload, None


def _probe(models: list[str]) -> None:
    """Send one short prompt to each model and print the resolved model ids."""
    status = get_account_status()
    if status is None:
        raise SystemExit("`claude` CLI is not logged in.")
    print(f"account: {status.get('email')} ({status.get('subscriptionType')})")
    for model in models:
        text, payload, error = invoke_once("Reply with the single word OK.",
                                           model, "You are terse.", timeout=120)
        resolved = list((payload or {}).get("modelUsage", {}))
        print(f"{model}: reply={text!r} error={error} resolved={resolved}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--probe", nargs="+",
                        default=["claude-sonnet-5", "claude-haiku-4-5-20251001"])
    _probe(parser.parse_args().probe)
