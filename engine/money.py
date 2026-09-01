"""
Integer money primitives.

HARD RULE (spec sec.0.1): all money is BIGINT paise. No float, no double, no
Decimal-with-float-construction, anywhere on the money path. All rates are
integer basis points (bps): 2% == 200, 18% == 1800.

`bps()` is the ONLY rounding function permitted in the money path. Never use
Python's builtin round() -- it is banker's rounding and will silently produce
off-by-one-paise drift that then gets diagnosed as a fake anomaly.
"""
from __future__ import annotations


def bps(amount_paise: int, rate_bps: int) -> int:
    """ROUND_HALF_UP of amount_paise * rate_bps / 10000, in integer paise.

    Pure integer arithmetic. Handles negative amounts by symmetry (round half
    away from zero), so refund-side computations stay sign-consistent.
    """
    if not isinstance(amount_paise, int) or not isinstance(rate_bps, int):
        raise TypeError("bps() operates on integers only (paise, bps)")
    if amount_paise < 0:
        return -((-amount_paise * rate_bps + 5000) // 10000)
    return (amount_paise * rate_bps + 5000) // 10000


def rupees(amount_paise: int) -> str:
    """Display boundary ONLY. Indian digit grouping, e.g. -123456789 -> '-12,34,567.89'."""
    if amount_paise is None:
        return ""
    neg = amount_paise < 0
    v = abs(int(amount_paise))
    whole, frac = divmod(v, 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{'-' if neg else ''}Rs {s}.{frac:02d}"


def assert_int_money(**kwargs) -> None:
    """Guard used at module boundaries to make a float leak fail loudly."""
    for name, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name}={value!r} is not integer paise ({type(value).__name__})")
