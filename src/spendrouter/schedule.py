"""Peak-hour and deferred-window awareness.

Two things move the price of the same work:

* Peak hours burn more subscription quota (community reports ~3-4x).
* Deferred-spend windows are the cheap slots, and they shift, so the router
  must consult a schedule table instead of assuming "off-peak = cheap".
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from .config import ProjectConfig


def parse_window(window: str) -> tuple[int, int]:
    """'09:30-17:00' -> (570, 1020) minutes since midnight."""

    start, end = window.split("-")

    def to_minutes(part: str) -> int:
        hh, mm = part.split(":")
        return int(hh) * 60 + int(mm)

    return to_minutes(start), to_minutes(end)


@dataclass(frozen=True)
class ScheduleState:
    """Where ``when`` sits relative to a project's peak/deferred schedule."""

    when: _dt.datetime
    is_peak: bool
    in_deferred_window: bool
    quota_multiplier: float
    deferred_discount: float
    note: str

    @property
    def hour(self) -> int:
        return self.when.hour


def evaluate(project: ProjectConfig, when: _dt.datetime | None = None) -> ScheduleState:
    """Classify ``when`` (default: now) against the project's schedule table."""

    moment = when or _dt.datetime.now().astimezone()
    is_peak = moment.hour in project.peak_hours
    minute_of_day = moment.hour * 60 + moment.minute

    in_window = False
    for window in project.deferred_windows:
        start, end = parse_window(window)
        if start <= end:
            hit = start <= minute_of_day < end
        else:  # window wraps past midnight, e.g. 22:00-02:00
            hit = minute_of_day >= start or minute_of_day < end
        if hit:
            in_window = True
            break

    quota_multiplier = project.peak_multiplier if is_peak else 1.0

    # Deferred pricing only applies when a window is open. A project that
    # declares no windows treats deferred spend as always available at list
    # price; a project that declares windows means "only inside these".
    if project.deferred_windows:
        deferred_discount = project.deferred_multiplier if in_window else 0.0
    else:
        deferred_discount = project.deferred_multiplier
        in_window = True

    if is_peak and in_window:
        note = "peak hour inside a deferred window: quota burns harder, deferred still discounted"
    elif is_peak:
        note = f"peak hour: subscription quota x{quota_multiplier:g}"
    elif in_window and project.deferred_windows:
        note = "deferred window open: cheapest slot of the day"
    elif in_window:
        note = "off-peak: normal quota burn"
    else:
        note = "outside every deferred window: deferred spend unavailable, subscription preferred"
    return ScheduleState(
        when=moment,
        is_peak=is_peak,
        in_deferred_window=in_window,
        quota_multiplier=quota_multiplier,
        deferred_discount=deferred_discount,
        note=note,
    )
