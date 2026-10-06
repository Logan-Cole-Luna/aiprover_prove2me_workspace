"""Render a run's trace.json as two self-contained, linked HTML pages.

trace.html (template `trace_view.html`) walks a reader chapter by chapter
through the problem, the orchestration algorithm, the prompts, and every
model call, Lean check and decision of the run. trace_replay.html (template
`trace_replay.html`) animates the run top to bottom on the trace's own clock, from the input
problem through the captain's steps and the parallel solver lanes to the
verified proof. Both pages share `trace_common.css`, `trace_common.js` and the
style in `trace_theme.css`. The trace is embedded verbatim, except that absolute
paths of Lean attempt files are shortened to their file names.

Usage:
    python3 -m orchestrator.trace_view results/<run_id>/trace.json
    # writes trace.html and trace_replay.html next to the trace
"""

import argparse
import html
import json
import re
from pathlib import Path

from .aiprover_agent import lean_check_summary, read_reasoning, read_session, session_ending

HERE = Path(__file__).parent
AIPROVER_JOBS = HERE.parent / "aiprover" / "work" / "jobs"
# Output file name → (template, page title suffix).
PAGES = {
    "trace.html": ("trace_view.html", "proof run"),
    "trace_replay.html": ("trace_replay.html", "orchestration replay"),
}
COMMON_CSS_MARKER = "/*TRACE_COMMON_CSS*/"
COMMON_JS_MARKER = "/*TRACE_COMMON_JS*/"
THEME_CSS_MARKER = "/*TRACE_THEME_CSS*/"
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


def add_sample_records(trace: dict) -> dict:
    """Fill in, for traces recorded before the pipeline stored them, why each
    AIProver session stopped, the harness's Lean check of its final file and
    the file itself, from the job's per-sample records if they still exist."""
    for step in trace.get("steps", []):
        job = step.get("aiprover_job")
        for sample in step.get("samples") or []:
            if not job or all(key in sample for key in ("ending", "check", "lean", "session")):
                continue
            summary_path = AIPROVER_JOBS / job / f"s{sample.get('sample')}" / "summary.json"
            if not summary_path.exists():
                continue
            summary = json.loads(summary_path.read_text())
            sample.setdefault("ending", session_ending(summary))
            sample.setdefault("check", lean_check_summary(summary.get("check")))
            lean_file = Path(summary.get("lean_file") or "")
            if "lean" not in sample and lean_file.is_file():
                sample["lean"] = lean_file.read_text(errors="replace")
            sample.setdefault("session", read_session(summary_path.parent))
            sample.setdefault("reasoning", read_reasoning(summary_path.parent))
    return trace


# Per-sample records shown only in the replay's step drawer; the other pages
# are rendered without them to stay small.
REPLAY_ONLY = ("lean", "session", "reasoning")


def without_sample_records(trace: dict) -> dict:
    steps = []
    for step in trace.get("steps", []):
        if step.get("samples"):
            step = {**step, "samples": [{key: value for key, value in sample.items()
                                         if key not in REPLAY_ONLY}
                                        for sample in step["samples"]]}
        steps.append(step)
    return {**trace, "steps": steps}


def render(trace_path: Path, output_dir: Path) -> list[Path]:
    trace = shorten_paths(add_sample_records(json.loads(trace_path.read_text())))
    # `</` would terminate the enclosing <script> element early.
    payloads = {replay: json.dumps(trace if replay else without_sample_records(trace),
                                   ensure_ascii=False).replace("</", "<\\/")
                for replay in (True, False)}
    common_css = (HERE / "trace_common.css").read_text()
    common_js = (HERE / "trace_common.js").read_text()
    theme_css = (HERE / "trace_theme.css").read_text()
    theorem = trace.get("theorem_name", "Orchestration")
    written = []
    for output_name, (template_name, title_suffix) in PAGES.items():
        page = (HERE / template_name).read_text()
        page = (page.replace(COMMON_CSS_MARKER, common_css)
                    .replace(COMMON_JS_MARKER, common_js)
                    .replace(THEME_CSS_MARKER, theme_css)
                    .replace(TITLE_MARKER, html.escape(f"{theorem} {title_suffix}"))
                    .replace(DATA_MARKER, payloads[output_name == "trace_replay.html"]))
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
