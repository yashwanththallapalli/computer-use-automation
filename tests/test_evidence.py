"""StepLog tests: written immediately (crash-survivable), one JSON object per
line, redacted with whatever secrets are known at write time."""

import json

from cua.evidence import StepLog


def test_write_appends_one_json_line_per_call(tmp_path):
    path = tmp_path / "steps.jsonl"
    with StepLog(path) as log:
        log.write({"index": 0, "action": "click"})
        log.write({"index": 1, "action": "fill"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rows = [json.loads(l) for l in lines]
    assert [r["index"] for r in rows] == [0, 1]
    assert all("ts" in r for r in rows)


def test_write_is_flushed_before_close(tmp_path):
    # the crash-survivability property: a reader opening the file in a
    # second handle sees the line without the writer having closed yet
    path = tmp_path / "steps.jsonl"
    log = StepLog(path)
    log.write({"index": 0})
    on_disk = path.read_text(encoding="utf-8")
    log.close()
    assert '"index": 0' in on_disk


def test_write_redacts_caller_data_but_not_the_timestamp(tmp_path):
    path = tmp_path / "steps.jsonl"
    with StepLog(path) as log:
        log.write({"balance": "4210.55"}, secrets=["4210.55"])
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert line["balance"] == "[REDACTED]"
    assert line["ts"].count("-") == 2  # untouched ISO date, not mangled


def test_append_mode_preserves_earlier_lines(tmp_path):
    path = tmp_path / "steps.jsonl"
    with StepLog(path) as log:
        log.write({"index": 0})
    with StepLog(path) as log:  # a second run appending to the same file
        log.write({"index": 1})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
