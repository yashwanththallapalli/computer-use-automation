"""Structured per-step evidence, shared by the discovery and replay loops.

Each step is appended immediately as one line of ``steps.jsonl`` -- unlike
``transcript.json`` / ``result.json`` (written once, at the end, with
guaranteed-complete redaction over the whole record), this survives a crash or
a killed process mid-run: whatever ran before the crash is already on disk.

Redaction here is best-effort with whatever secrets are known at the moment a
line is written (a sensitive value is known from the step that extracts it
onward, so it's caught from there on); the final transcript/result written at
the end of a successful run is still the authoritative, fully-redacted copy.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .redaction import redact_mapping


class StepLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def write(self, record: dict, secrets: list[str] | None = None) -> None:
        # redact only the caller's data, never metadata this class adds --
        # a bare ISO timestamp otherwise matches the DOB pattern (YYYY-MM-DD)
        redacted = redact_mapping(record, secrets) if secrets else record
        line = {"ts": datetime.now(timezone.utc).isoformat(), **redacted}
        self._fh.write(json.dumps(line, default=str) + "\n")
        self._fh.flush()  # crash-survivable: on disk before the next step runs

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "StepLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
