"""Render a run's trace.json as three self-contained, linked HTML pages.

trace.html (template `trace_view.html`) walks a reader chapter by chapter
through the problem, the orchestration algorithm, the prompts, and every
model call, Lean check and decision of the run. trace_graph.html (template
`trace_graph.html`) shows the same run as a pannable, zoomable graph: the
captain, the subtasks it performs or delegates, the agents assigned to each,
and their attempts in order. trace_replay.html (template `trace_replay.html`)
animates the run top to bottom on the trace's own clock, from the input
problem through the captain's steps and the parallel solver lanes to the
verified proof. All pages share `trace_common.css` and `trace_common.js`. The trace is embedded verbatim, except that absolute
paths of Lean attempt files are shortened to their file names.

Usage:
    python3 -m orchestrator.trace_view results/<run_id>/trace.json
    # writes trace.html, trace_graph.html, trace_replay.html next to the trace
"""

import argparse
import html
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
# Output file name → (template, page title suffix).
PAGES = {
    "trace.html": ("trace_view.html", "proof run"),
    "trace_graph.html": ("trace_graph.html", "orchestration graph"),
    "trace_replay.html": ("trace_replay.html", "orchestration replay"),
}
COMMON_CSS_MARKER = "/*TRACE_COMMON_CSS*/"
COMMON_JS_MARKER = "/*TRACE_COMMON_JS*/"
DATA_MARKER = "/*TRACE_DATA*/null"
TITLE_MARKER = "__PAGE_TITLE__"

# Absolute paths such as /home/.../temp/<run_id>/0004_lemma_w0_r0.lean.
LEAN_FILE_PATH = re.compile(r"(?:/[\w.\-]+)+/([\w.\-]+\.lean)")


def shorten_paths(value):
    """Replace absolute Lean file paths with their base names, recursively."""
    if isinstance(value, str):
        return LEAN_FILE_PATH.sub(r"\1", value)
    if isinstance(value, list):
        return [shorten_paths(item) for item in value]
    if isinstance(value, dict):
        return {key: shorten_paths(item) for key, item in value.items()}
    return value


def render(trace_path: Path, output_dir: Path) -> list[Path]:
    trace = shorten_paths(json.loads(trace_path.read_text()))
    # `</` would terminate the enclosing <script> element early.
    payload = json.dumps(trace, ensure_ascii=False).replace("</", "<\\/")
    common_css = (HERE / "trace_common.css").read_text()
    common_js = (HERE / "trace_common.js").read_text()
    theorem = trace.get("theorem_name", "Orchestration")
    written = []
    for output_name, (template_name, title_suffix) in PAGES.items():
        page = (HERE / template_name).read_text()
        page = (page.replace(COMMON_CSS_MARKER, common_css)
                    .replace(COMMON_JS_MARKER, common_js)
                    .replace(TITLE_MARKER, html.escape(f"{theorem} {title_suffix}"))
                    .replace(DATA_MARKER, payload))
        output_path = output_dir / output_name
        output_path.write_text(page)
        written.append(output_path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output-dir", type=Path,
                        help="default: the directory containing the trace")
    arguments = parser.parse_args()
    output_dir = arguments.output_dir or arguments.trace.parent
    for path in render(arguments.trace, output_dir):
        print(path)


if __name__ == "__main__":
    main()
