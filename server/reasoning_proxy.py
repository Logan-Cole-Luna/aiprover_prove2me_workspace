"""Records the AIProver model's reasoning, which the harness discards.

vLLM returns a model's reasoning in the `reasoning` field of each completion;
the harness's agent (vibe) reads `reasoning_content` and drops it. This proxy
sits between the harness and the model endpoint (the SSH tunnel), forwards
every request and response unchanged, and appends the reasoning of each
chat completion to `<jobs>/<job>/s<k>/reasoning.jsonl`, the directory of the
session that made the request (read from the task message, which names it).

Start (single process):
    python3 -m server.reasoning_proxy --listen 18565 --upstream http://127.0.0.1:18555

A record per completion: {"turn", "time", "reasoning", "content_chars",
"tool_calls", "truncated"}, where `turn` counts the assistant messages
including this one, so it matches the turn numbers of the session transcript,
and `truncated` marks a reply stopped by the token cap.

The harness's agent sends no `max_tokens`, so a reply may run to the context
limit; a reply that does not converge then holds its session until the
session's clock. The proxy sets `max_tokens` (reasoning and answer together)
on chat requests that carry none. The default, 24,576 tokens, is above every
completed reply but a handful of the 10,823 recorded (99.9th percentile about
57,000 characters, ~16,000 tokens) and takes about 14 min at the 29 tokens/s
per request of 8 concurrent sessions, within the 90-min session clock. A
capped reply without a tool call ends the agent's turn; the harness's answer
check then returns the Lean errors and the session continues.

vLLM rejects a request (400) whose history holds a tool call with arguments
that are not JSON or a name outside [a-zA-Z0-9_-]{1,64}. The model emits such
calls in about 1 of 100 sessions; the harness already answered them with a
tool error, but resends them, and the 400 restarts the session from scratch.
The proxy replaces such arguments with `{}` and such names with a sanitized
form before forwarding.
"""

import argparse
import asyncio
import json
import logging
import re
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "aiprover" / "work" / "jobs"
# The task message names the session's project: .../jobs/<job>/s<k>/proj/...
SESSION_PATH = re.compile(r"/jobs/([^/\s\"']+)/s(\d+)/")
HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive"}
MAX_REPLY_TOKENS = 24576
TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

logger = logging.getLogger("reasoning_proxy")


def session_of(request_body: dict) -> tuple[Path, int] | None:
    """The sample directory of a chat request and the turn it asks for."""
    messages = request_body.get("messages") or []
    for message in messages:
        match = SESSION_PATH.search(str(message.get("content") or ""))
        if match:
            directory = JOBS / match.group(1) / f"s{match.group(2)}"
            turn = sum(m.get("role") == "assistant" for m in messages) + 1
            return directory, turn
    return None


def repair_tool_calls(request_body: dict) -> int:
    """Make past tool calls acceptable to vLLM; return how many were changed."""
    repaired = 0
    for message in request_body.get("messages") or []:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    json.loads(arguments)
                except json.JSONDecodeError:
                    function["arguments"] = "{}"
                    repaired += 1
            name = function.get("name")
            if isinstance(name, str) and not TOOL_NAME.match(name):
                function["name"] = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64] or "invalid"
                repaired += 1
    return repaired


def record(directory: Path, turn: int, reasoning: str, content: str, tool_calls: int,
           truncated: bool) -> None:
    if not directory.is_dir():
        return
    entry = {"turn": turn, "time": time.strftime("%H:%M:%S"), "reasoning": reasoning,
             "content_chars": len(content), "tool_calls": tool_calls, "truncated": truncated}
    with open(directory / "reasoning.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Accumulator:
    """Collects reasoning and content from a streamed (SSE) completion."""

    def __init__(self):
        self.buffer = b""
        self.reasoning, self.content = [], []
        self.tool_call_ids = set()
        self.finish_reason = None

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:") or line == b"data: [DONE]":
                continue
            try:
                event = json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            for choice in event.get("choices") or []:
                self.finish_reason = choice.get("finish_reason") or self.finish_reason
                delta = choice.get("delta") or {}
                self.reasoning.append(delta.get("reasoning") or delta.get("reasoning_content") or "")
                self.content.append(delta.get("content") or "")
                for call in delta.get("tool_calls") or []:
                    self.tool_call_ids.add(call.get("index", call.get("id")))


async def forward(request: web.Request) -> web.StreamResponse:
    upstream = request.app["upstream"]
    body = await request.read()
    session = None
    if request.method == "POST" and request.path.endswith("/chat/completions"):
        try:
            payload = json.loads(body)
            session = session_of(payload)
            changed = repair_tool_calls(payload)
            if changed:
                logger.info(f"repaired {changed} malformed tool call(s) in the history")
            if not (payload.get("max_tokens") or payload.get("max_completion_tokens")):
                payload["max_tokens"] = request.app["max_reply_tokens"]
                changed = True
            if changed:
                body = json.dumps(payload).encode()
        except (json.JSONDecodeError, AttributeError):
            session = None
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    client: ClientSession = request.app["client"]
    async with client.request(request.method, upstream + request.path_qs, data=body,
                              headers=headers) as reply:
        response = web.StreamResponse(status=reply.status, headers={
            k: v for k, v in reply.headers.items() if k.lower() not in HOP_HEADERS})
        await response.prepare(request)
        streamed = "text/event-stream" in reply.headers.get("content-type", "")
        accumulator = Accumulator()
        whole = b""
        async for chunk in reply.content.iter_any():
            await response.write(chunk)
            if session and streamed:
                accumulator.feed(chunk)
            elif session:
                whole += chunk
        await response.write_eof()
    if session and reply.status == 200:
        directory, turn = session
        if streamed:
            record(directory, turn, "".join(accumulator.reasoning), "".join(accumulator.content),
                   len(accumulator.tool_call_ids), accumulator.finish_reason == "length")
        else:
            try:
                choice = json.loads(whole)["choices"][0]
                message = choice["message"]
                record(directory, turn,
                       message.get("reasoning") or message.get("reasoning_content") or "",
                       message.get("content") or "", len(message.get("tool_calls") or []),
                       choice.get("finish_reason") == "length")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
    return response


async def start(app: web.Application) -> None:
    # Model turns can take many minutes; no total timeout.
    app["client"] = ClientSession(timeout=ClientTimeout(total=None, sock_connect=30))


async def stop(app: web.Application) -> None:
    await app["client"].close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen", type=int, default=18565)
    parser.add_argument("--upstream", default="http://127.0.0.1:18555")
    parser.add_argument("--max-reply-tokens", type=int, default=MAX_REPLY_TOKENS,
                        help="max_tokens set on chat requests that carry none")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application(client_max_size=256 * 1024 ** 2)
    app["upstream"] = arguments.upstream.rstrip("/")
    app["max_reply_tokens"] = arguments.max_reply_tokens
    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    app.router.add_route("*", "/{tail:.*}", forward)
    web.run_app(app, host="127.0.0.1", port=arguments.listen, access_log=None)


if __name__ == "__main__":
    main()
