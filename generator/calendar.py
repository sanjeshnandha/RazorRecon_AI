"""Working-day calendar driven entirely by policy.yaml.

Non-working days per POLICY.SETTLEMENT.*:
  - Sundays
  - the 2nd and 4th Saturday of the month
  - listed bank holidays
Saturdays 1, 3 and 5 of a month ARE working days.
"""
from __future__ import annotations

from datetime import date, timedelta

from engine.policy import Policy


def _nth_saturday_of_month(d: date) -> int:
    """For a Saturday, which Saturday of the month it is (1-based)."""
    return (d.day - 1) // 7 + 1


def is_working_day(d: date, policy: Policy) -> bool:
    if d in policy.holidays:
        return False
    wd = d.weekday()  # Mon=0 .. Sun=6
    if wd == 6 and policy.exclude_sundays:
        return False
    if wd == 5 and policy.exclude_second_fourth_saturday:
        if _nth_saturday_of_month(d) in (2, 4):
            return False
    return True


def add_working_days(start: date, n: int, policy: Policy) -> date:
    """Add n working days to `start`. add_working_days(d, 0) rolls forward to
    the next working day if `d` itself is not one."""
    d = start
    while not is_working_day(d, policy):
        d += timedelta(days=1)
    remaining = n
    while remaining > 0:
        d += timedelta(days=1)
        if is_working_day(d, policy):
            remaining -= 1
    return d


def working_days_between(a: date, b: date, policy: Policy) -> int:
    """Signed count of working days from a to b (exclusive of a, inclusive of b)."""
    if b == a:
        return 0
    step = 1 if b > a else -1
    d, n = a, 0
    while d != b:
        d += timedelta(days=step)
        if is_working_day(d, policy):
            n += step
    return n


def next_working_day(d: date, policy: Policy) -> date:
    return add_working_days(d, 1, policy)
