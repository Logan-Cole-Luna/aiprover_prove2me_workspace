"""Step-by-step record of an orchestration run, for later replay and display.

The trace is one JSON document: a header (problem, models, configuration,
algorithm description, system prompts) followed by an ordered list of steps.
Each step is a model call, a Lean check, or an orchestration decision. The
file is rewritten atomically after every step so it can be inspected while
the run is in progress.
"""

import json
import os
import threading
import time
from pathlib import Path


class Trace:

    def __init__(self, path: Path, header: dict, previous: dict | None = None):
        """Start a trace, or continue `previous` (a loaded trace document).

        A continued trace keeps its steps and its clock: elapsed times resume
        from the last recorded step, and each resumption is listed in
        `resumptions`.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.stage = "setup"      # coarse pipeline phase, set by the pipeline
        if previous is None:
            self._start = time.time()
            self.document = {**header, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "steps": [], "outcome": None}
        else:
            steps = previous.get("steps", [])
            elapsed = steps[-1]["elapsed_seconds"] if steps else 0.0
            self._start = time.time() - elapsed
            resumptions = previous.get("resumptions", []) + [{
                "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "at_step": len(steps),
                "elapsed_seconds": elapsed,
                "previous_outcome": previous.get("outcome")}]
            self.document = {**previous, **header, "started": previous.get("started"),
                             "steps": steps, "outcome": None, "resumptions": resumptions}
        self._write()

    @staticmethod
    def load(path: Path) -> dict | None:
        path = Path(path)
        return json.loads(path.read_text()) if path.exists() else None

    @property
    def elapsed_seconds(self) -> float:
        return round(time.time() - self._start, 1)

    def add(self, kind: str, **data) -> int:
        """Append a step, tagged with the current stage, and return its index."""
        with self._lock:
            index = len(self.document["steps"])
            self.document["steps"].append({
                "index": index,
                "kind": kind,
                "stage": self.stage,
                "elapsed_seconds": round(time.time() - self._start, 1),
                **data,
            })
            self._write()
            return index

    def finish(self, outcome: dict) -> None:
        with self._lock:
            self.document["outcome"] = outcome
            self.document["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._write()

    def _write(self) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.document, indent=1, ensure_ascii=False))
        os.replace(temporary, self.path)
