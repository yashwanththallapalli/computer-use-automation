"""Engine-level guardrail defaults, kept as reviewable data (not buried in
code) at config/guardrails.json. A per-capability artifact's own
``policy.allowed_url_patterns`` still applies on top of this at replay time --
this is the floor discovery runs against before any artifact exists.
"""

from __future__ import annotations

import json
from pathlib import Path

_DEFAULTS = {
    "allowed_url_patterns": [r"^http://127\.0\.0\.1:5001/members(/|$|\?)"],
    "denylist_patterns": [r"/admin(/|$)"],
}

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "guardrails.json"


def load_guardrail_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if path.exists():
        return {**_DEFAULTS, **json.loads(path.read_text(encoding="utf-8"))}
    return dict(_DEFAULTS)
