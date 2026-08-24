"""Counters that show what Hue Insist is actually doing.

A watchdog you cannot observe is indistinguishable from one that is broken. These
counters answer two separate questions: is it running at all, and is there
anything worth fixing.

The ratio between `devices_checked` and `no_correction_needed` is the interesting
one. It turns "I think that lamp misses commands sometimes" into a number, and it
points at which lamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Stats:
    """Running totals for one Home Assistant session."""

    # Requests caught, split by what was aimed at. A single request can be
    # counted in more than one category when it targets a group and a loose lamp
    # at the same time.
    actions_total: int = 0
    actions_scene: int = 0
    actions_group: int = 0
    actions_device: int = 0

    # Lamp-level checks. Counted once per request, after expanding scenes and
    # groups -- not once per retry round, otherwise a single stubborn lamp would
    # inflate the total and ruin the ratio.
    devices_checked: int = 0
    devices_ok: int = 0

    corrections: int = 0
    failures: int = 0

    last_action: datetime | None = None
    last_correction: datetime | None = None
    last_failure: datetime | None = None
    last_failed_entities: list[str] = field(default_factory=list)
