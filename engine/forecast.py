"""
Forward cash position.

Reconciliation looks backwards: what happened, and does it add up. This looks
forward: on which working day does how much money actually arrive, and how much
goes back out.

It is a DERIVATION, not a prediction. Every date below comes from the working-day
calendar and the policy registry, so each row can cite the rule that dated it.
Nothing here averages history or fits a model -- an estimate you cannot defend to
a controller is worth less than no estimate.

Three sources, and one deliberate refusal:

  * settlements the matcher could not match  -> inflow, on their due date
  * payments captured but not yet settled    -> inflow, via projected settlement
  * allocations owed but not yet transferred -> outflow

The refusal: this module never decides for itself whether a settlement was
matched. It reads the D2 verdict the engine already recorded. Re-deriving
"unmatched" with its own SQL is how a forecaster ends up confidently reporting
money that is already in the bank -- on the demo dataset a naive UTR join says
Rs 1.2 Cr is in flight when the true figure is Rs 3.8 L, because 20 of those 21
settlements were matched on evidence other than the UTR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from engine.db import fetch, fetch_one
from engine.policy import Policy, load_policy
from generator.calendar import add_working_days, is_working_day, working_days_between

DEFAULT_HORIZON = 15          # working days


# =============================================================================
# Due dates
# =============================================================================
def credit_due_date(settlement_date: date, policy: Policy) -> date:
    """When a settlement's bank credit is contractually due.

    settlement_date + POLICY.BANK.expected_lag_days working days, then the
    tolerance window. Weekends and the listed bank holidays are skipped, which
    is the whole reason this cannot be date arithmetic.
    """
    due = add_working_days(settlement_date, policy.expected_lag_days, policy)
    return add_working_days(due, policy.bank_tolerance_days, policy)


def settlement_status(settlement_date: date, as_of: date, policy: Policy) -> dict:
    """AWAITED or OVERDUE, with the working days either way.

    This is the distinction the flat 'missing credit' exception cannot make, and
    the one a controller actually asks about: is this money late, or is it gone?
    """
    due = credit_due_date(settlement_date, policy)
    if as_of <= due:
        return {"state": "AWAITED", "due_date": due.isoformat(),
                "working_days_until_due": max(0, working_days_between(as_of, due, policy)),
                "rule": f"POLICY.BANK.expected_lag_days@{policy.version} "
                        f"+ POLICY.BANK.tolerance_days@{policy.version}"}
    return {"state": "OVERDUE", "due_date": due.isoformat(),
            "working_days_overdue": working_days_between(due, as_of, policy),
            "rule": f"POLICY.BANK.expected_lag_days@{policy.version} "
                    f"+ POLICY.BANK.tolerance_days@{policy.version}"}


def projected_settlement_date(capture_day: date, policy: Policy) -> date:
    """A captured payment settles on the T+n working day after its period closes.
    With no period open yet for it, the capture day itself is the cutoff."""
    return add_working_days(capture_day, policy.cycle_working_days, policy)


# =============================================================================
# The forecast
# =============================================================================
@dataclass
class Line:
    """One dated movement, with the record and the rule behind it."""
    date: str
    direction: str            # IN | OUT
    bucket: str
    amount_paise: int
    subject_type: str
    subject_id: str
    basis: str
    rule: str = ""
    state: str = ""
    # Structured facts behind `basis`, so a UI can build columns instead of
    # parsing prose. Shape varies by bucket; always JSON-safe scalars.
    detail: dict = field(default_factory=dict)


@dataclass
class Forecast:
    as_of: str
    horizon_working_days: int
    lines: list[Line] = field(default_factory=list)
    days: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    overdue: list[dict] = field(default_factory=list)
    overdue_payouts: list[dict] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


def _as_of(conn, dataset_id: str) -> date:
    """The book's own present: the day after the last settlement period closed.
    Wall-clock time is meaningless against a dataset dated 2026."""
    row = fetch_one(conn, "SELECT MAX(settlement_period_end) AS e FROM settlements "
                          "WHERE dataset_id=%s", (dataset_id,))
    return (row["e"] + timedelta(days=1)) if row and row["e"] else date.today()


def build(conn, run_id: str, dataset_id: str, as_of: date | None = None,
          horizon: int = DEFAULT_HORIZON) -> Forecast:
    policy = load_policy()
    as_of = as_of or _as_of(conn, dataset_id)
    horizon = max(1, min(int(horizon), 60))
    last_day = add_working_days(as_of, horizon, policy)

    f = Forecast(as_of=as_of.isoformat(), horizon_working_days=horizon)
    f.assumptions = [
        f"Bank credit is due {policy.expected_lag_days} working day(s) after the "
        f"settlement date, plus {policy.bank_tolerance_days} day(s) tolerance "
        f"(POLICY.BANK@{policy.version}).",
        f"A captured payment settles T+{policy.cycle_working_days} working days "
        f"(POLICY.SETTLEMENT.cycle_working_days@{policy.version}).",
        "Seller payouts are dated at the settlement that funds them: the policy "
        "defines no separate payout lag, and a marketplace cannot pay out money "
        "it has not yet received. Stated rather than assumed silently.",
        "Sundays, 2nd and 4th Saturdays and the listed bank holidays are not "
        "working days, so every date here skips them.",
    ]

    # ---- 1. settlements the MATCHER could not match -------------------------
    # The verdict is read, never recomputed. See the module docstring.
    for r in fetch(conn, """
            SELECT d.settlement_id, d.residual_paise, s.settlement_date
            FROM reconciliation_deltas d
            JOIN settlements s ON s.dataset_id=%s AND s.settlement_id=d.settlement_id
            WHERE d.run_id=%s AND d.delta_kind='D2_BANK' AND d.residual_paise > 0
            ORDER BY s.settlement_date""", (dataset_id, run_id)):
        st = settlement_status(r["settlement_date"], as_of, policy)
        line = Line(date=st["due_date"], direction="IN", bucket="settlement_awaited",
                    amount_paise=int(r["residual_paise"]), subject_type="settlement",
                    subject_id=r["settlement_id"],
                    basis=f"settled {r['settlement_date'].isoformat()}, credit due "
                          f"{st['due_date']}", rule=st["rule"], state=st["state"],
                    detail={"settlement_date": r["settlement_date"].isoformat(),
                            "due_date": st["due_date"],
                            "working_days_until_due": st.get("working_days_until_due")})
        if st["state"] == "OVERDUE":
            f.overdue.append({**st, "settlement_id": r["settlement_id"],
                              "amount_paise": int(r["residual_paise"]),
                              "settlement_date": r["settlement_date"].isoformat()})
        else:
            f.lines.append(line)

    # ---- 2. captured, not yet settled --------------------------------------
    for r in fetch(conn, """
            SELECT p.payment_id, p.amount_paise, p.payment_method,
                   p.captured_at::date AS capture_day
            FROM payments p
            WHERE p.dataset_id=%s AND p.payment_status='CAPTURED'
              AND NOT EXISTS (SELECT 1 FROM settlement_items si
                              WHERE si.dataset_id=p.dataset_id
                                AND si.payment_id=p.payment_id
                                AND si.transaction_type='PAYMENT')
            ORDER BY p.captured_at""", (dataset_id,)):
        settles = projected_settlement_date(r["capture_day"], policy)
        lands = credit_due_date(settles, policy)
        fee = _fee_after_policy(r["amount_paise"], r["payment_method"], policy)
        f.lines.append(Line(
            date=lands.isoformat(), direction="IN", bucket="pipeline",
            amount_paise=r["amount_paise"] - fee, subject_type="payment",
            subject_id=r["payment_id"],
            basis=f"captured {r['capture_day'].isoformat()}, settles "
                  f"{settles.isoformat()} (T+{policy.cycle_working_days}), net of "
                  f"{r['payment_method']} fee and GST",
            rule=f"POLICY.MDR.{r['payment_method']}@{policy.version}", state="PROJECTED",
            detail={"capture_date": r["capture_day"].isoformat(),
                    "settles_on": settles.isoformat(),
                    "credit_due": lands.isoformat(),
                    "method": r["payment_method"],
                    "gross_paise": int(r["amount_paise"]),
                    "fee_paise": int(fee),
                    "cycle_working_days": policy.cycle_working_days,
                    "working_days_until_credit": working_days_between(as_of, lands, policy)}))

    # ---- 3. owed to sellers, not yet transferred ---------------------------
    for r in fetch(conn, """
            SELECT a.allocation_id, a.seller_id, a.net_seller_paise,
                   p.captured_at::date AS capture_day, sl.seller_name
            FROM seller_allocations a
            JOIN payments p ON p.dataset_id=a.dataset_id AND p.payment_id=a.payment_id
            JOIN sellers sl ON sl.dataset_id=a.dataset_id AND sl.seller_id=a.seller_id
            WHERE a.dataset_id=%s AND a.allocation_status='PENDING'
            ORDER BY p.captured_at""", (dataset_id,)):
        due = credit_due_date(projected_settlement_date(r["capture_day"], policy), policy)
        if due < as_of:
            # The settlement that funds this has already been and gone, and the
            # seller still has not been paid. That is money owed now, not money
            # scheduled -- putting it on a future day would understate what the
            # merchant already owes.
            f.overdue_payouts.append({
                "allocation_id": r["allocation_id"], "seller_id": r["seller_id"],
                "seller_name": r["seller_name"],
                "amount_paise": int(r["net_seller_paise"]),
                "due_date": due.isoformat(),
                "working_days_overdue": working_days_between(due, as_of, policy)})
            continue
        f.lines.append(Line(
            date=due.isoformat(), direction="OUT", bucket="seller_payout",
            amount_paise=int(r["net_seller_paise"]), subject_type="seller_allocation",
            subject_id=r["allocation_id"],
            basis=f"{r['seller_name']} owed on the settlement funding "
                  f"{r['capture_day'].isoformat()}",
            rule=f"POLICY.SETTLEMENT.cycle_working_days@{policy.version}",
            state="PENDING",
            detail={"seller_id": r["seller_id"], "seller_name": r["seller_name"],
                    "capture_date": r["capture_day"].isoformat(),
                    "due_date": due.isoformat()}))

    _roll_up(f, as_of, last_day, policy)
    return f


def _fee_after_policy(amount: int, method: str, policy: Policy) -> int:
    """What the gateway will take. Projected inflow is net, because the gross
    never reaches the bank."""
    from engine.money import bps
    fee = bps(amount, policy.mdr_bps(method))
    return fee + bps(fee, policy.gst_on_fee_bps)


def _roll_up(f: Forecast, as_of: date, last_day: date, policy: Policy) -> None:
    """Day-by-day, working days only, with a running balance."""
    in_window = [l for l in f.lines if as_of <= date.fromisoformat(l.date) <= last_day]
    by_day: dict[str, dict] = {}
    for l in in_window:
        d = by_day.setdefault(l.date, {"date": l.date, "in_paise": 0, "out_paise": 0,
                                       "lines": 0})
        d["in_paise" if l.direction == "IN" else "out_paise"] += l.amount_paise
        d["lines"] += 1

    running, days = 0, []
    cur = as_of
    while cur <= last_day:
        if is_working_day(cur, policy):
            d = by_day.get(cur.isoformat(), {"date": cur.isoformat(), "in_paise": 0,
                                             "out_paise": 0, "lines": 0})
            d["net_paise"] = d["in_paise"] - d["out_paise"]
            running += d["net_paise"]
            d["running_paise"] = running
            days.append(d)
        cur += timedelta(days=1)
    f.days = days

    def total(bucket=None, direction=None):
        return sum(l.amount_paise for l in in_window
                   if (bucket is None or l.bucket == bucket)
                   and (direction is None or l.direction == direction))

    f.totals = {
        "inflow_paise": total(direction="IN"),
        "outflow_paise": total(direction="OUT"),
        "net_paise": total(direction="IN") - total(direction="OUT"),
        "settlement_awaited_paise": total(bucket="settlement_awaited"),
        "pipeline_paise": total(bucket="pipeline"),
        "seller_payout_paise": total(bucket="seller_payout"),
        "overdue_paise": sum(o["amount_paise"] for o in f.overdue),
        "overdue_count": len(f.overdue),
        "overdue_payout_paise": sum(o["amount_paise"] for o in f.overdue_payouts),
        "overdue_payout_count": len(f.overdue_payouts),
        "lines_in_window": len(in_window),
        "window_ends": last_day.isoformat(),
    }


def to_dict(f: Forecast) -> dict:
    window_end = f.totals.get("window_ends", "9999-12-31")
    inside = [l for l in f.lines if f.as_of <= l.date <= window_end]
    return {"as_of": f.as_of, "horizon_working_days": f.horizon_working_days,
            "totals": f.totals, "days": f.days,
            "overdue": sorted(f.overdue, key=lambda o: -o["amount_paise"]),
            "overdue_payouts": sorted(f.overdue_payouts,
                                      key=lambda o: -o["amount_paise"])[:50],
            "lines": [l.__dict__ for l in sorted(inside, key=lambda l: (l.date, l.bucket))],
            "assumptions": f.assumptions}
