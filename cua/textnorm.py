"""Shared text normalization used by both synthesis and replay.

Digit-bearing tokens (ids, amounts, dates) are treated as variable data, not
part of the semantic message -- stripped out before comparing a banner/heading
substring, so a checkpoint built from one run's "Sub-account 101445-01 was
opened successfully." still matches "Sub-account 999999-03 was opened
successfully." on a different run.
"""

import re


def strip_ids(text: str) -> str:
    return re.sub(r"\s{2,}", " ", re.sub(r"\S*\d\S*", "", text)).strip()
