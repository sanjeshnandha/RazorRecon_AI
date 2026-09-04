"""
The tax-line matcher: input tax credit, reconciled across three independent
sources.

WHY THIS IS A SEPARATE QUESTION FROM DELTA-1
Delta-1 already proves you were CHARGED the right GST: it recomputes the fee and
the tax from the policy registry and compares them against what the settlement
report claims. That is a complete answer to "did the gateway bill me correctly".

It is not an answer to "can I actually get that money back". Input tax credit is
only claimable if the supplier FILED a tax invoice that reaches the merchant's
GSTR-2B -- the statement the GST portal auto-drafts each month from suppliers'
own returns. A settlement can be flawless on all four deltas while the credit on
its fees is unclaimable, and nothing in the four deltas would notice. Past the
claim deadline that money is gone permanently, which makes it one of the few
reconciliation gaps that is a real, irreversible cash loss.

THE THREE SOURCES

    CHARGED    settlement_items.tax_paise      what the gateway billed
    BOOKED     the INPUT_GST ledger postings   what the merchant recorded as recoverable
    CLAIMABLE  tax_invoices (GSTR-2B)          what the portal will actually allow

Three answers to one question, from three parties who do not talk to each other.
Reported separately, in that order, and never blended -- the same discipline the
four deltas follow, for the same reason: a single number would hide which of the
three is wrong, and they have completely different remedies. CHARGED vs BOOKED is
your accountant's problem; BOOKED vs CLAIMABLE is your supplier's.

WHAT THIS MODULE IS NOT
It does not write. It does not touch reconciliation_deltas, exceptions, tiers or
any accuracy metric, and no run's config_hash changes because of it -- it carries
its own versioned registry, policy/tax.yaml. Like the forecaster, it is computed
on demand from persisted results and reads the engine's verdicts rather than
re-deriving them.

EVERY FIGURE HERE IS SYNTHETIC. The GSTR-2B feed is authored for this project.
It is not real filing data, and nothing here is tax advice.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from engine.db import fetch, fetch_one
from engine.money import bps

TAX_POLICY_PATH = str(Path(__file__).resolve().parent.parent / "policy" / "tax.yaml")


# =============================================================================
# Registry
# =============================================================================
class TaxPolicy:
    """policy/tax.yaml, with its own hash. Deliberately not merged into the
    reconciliation registry: that file's hash is stamped on every run ever made,
    and adding keys to it would invalidate the provenance of all of them."""

    def __init__(self, doc: dict, raw: str):
        self.version: str = doc["version"]
        self.config_hash: str = hashlib.sha256(raw.encode()).hexdigest()[:16]
        self.supplier_gstin: str = doc["supplier_gstin"]
        self.supplier_legal_name: str = doc["supplier_legal_name"]
        self.supplier_state_code: str = str(doc["supplier_state_code"])
        self.merchant_gstin: str = doc["merchant_gstin"]
        self.merchant_legal_name: str = doc["merchant_legal_name"]
        self.merchant_state_code: str = str(doc["merchant_state_code"])
        self.gst_on_fee_bps: int = int(doc["gst_on_fee_bps"])
        self.amount_tolerance_paise: int = int(doc["amount_tolerance_paise"])
        self.claim_window: int = int(doc["claim_window_return_periods"])

    @property
    def intra_state(self) -> bool:
        """Place of supply. Same state means CGST+SGST; different means IGST.
        Getting this wrong does not change the amount -- it changes which heads
        the credit sits under, and credit under the wrong head does not offset."""
        return self.supplier_state_code == self.merchant_state_code

    @property
    def expected_heads(self) -> str:
        return "CGST+SGST" if self.intra_state else "IGST"


def load_tax_policy(path: str = TAX_POLICY_PATH) -> TaxPolicy:
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    return TaxPolicy(yaml.safe_load(raw), raw)


# =============================================================================
# Outcomes
# =============================================================================
# Ordered worst-first. `claim_state` is the thing a controller acts on:
#   CLAIMABLE   the credit is available; nothing to do
#   DEFERRED    available, but in a later return period than expected
#   AT_RISK     not available as things stand, and someone must act
#   BLOCKED     the portal says it is not creditable at all; not an error, a fact
# The FILING verdict: booked vs what GSTR-2B will actually allow. Worst first.
STATUS_ORDER = ["NOT_FILED", "SPLIT_MISMATCH", "AMOUNT_MISMATCH", "ITC_BLOCKED",
                "PERIOD_MISMATCH", "MATCHED"]

CLAIM_STATE = {
    "NOT_FILED":       "AT_RISK",
    "SPLIT_MISMATCH":  "AT_RISK",
    "AMOUNT_MISMATCH": "AT_RISK",
    "ITC_BLOCKED":     "BLOCKED",
    "PERIOD_MISMATCH": "DEFERRED",
    "MATCHED":         "CLAIMABLE",
}

# The BOOKS verdict: charged vs what the merchant posted to INPUT_GST. Kept
# entirely separate from the filing verdict and never folded into it -- the two
# have different culprits and different remedies. A settlement whose books are
# wrong AND whose invoice was never filed has two problems, and collapsing them
# into one status would hide whichever came second in the code.
BOOKS_OK = "OK"
BOOKS_MISMATCH = "MISMATCH"


@dataclass
class TaxLine:
    """One settlement's tax position across all three sources."""
    settlement_id: str
    settlement_date: str
    return_period: str                 # the period this settlement belongs in
    charged_fee_paise: int
    charged_tax_paise: int
    booked_tax_paise: int
    claimable_tax_paise: int
    invoice_no: str | None
    invoice_period: str | None
    heads: str | None                  # CGST+SGST | IGST | None
    status: str                        # the FILING verdict
    claim_state: str
    at_risk_paise: int                 # credit that will not be realised
    rule: str
    finding: str
    books_status: str = BOOKS_OK       # the BOOKS verdict, independent of the above
    books_delta_paise: int = 0         # booked - charged; signed, never blended
    books_finding: str = ""
    evidence: list = field(default_factory=list)


@dataclass
class TaxReport:
    tax_policy_version: str
    tax_config_hash: str
    supplier_gstin: str
    merchant_gstin: str
    place_of_supply: str
    lines: list = field(default_factory=list)
    periods: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    installed: bool = True


def _period(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _period_gap(a: str, b: str) -> int:
    """Whole months from period `a` to period `b`. Negative means b is earlier."""
    ya, ma = int(a[:4]), int(a[5:7])
    yb, mb = int(b[:4]), int(b[5:7])
    return (yb - ya) * 12 + (mb - ma)


def table_installed(conn) -> bool:
    """db/tax.sql is idempotent but optional -- an older install may not have run
    it. The caller degrades rather than 500ing, the same way the agent does when
    agent_transcripts is missing."""
    row = fetch_one(conn, "SELECT to_regclass('public.tax_invoices') AS t")
    return bool(row and row["t"])


# =============================================================================
# The match
# =============================================================================
def build(conn, run_id: str, dataset_id: str, policy: TaxPolicy | None = None) -> TaxReport:
    p = policy or load_tax_policy()
    rep = TaxReport(tax_policy_version=p.version, tax_config_hash=p.config_hash,
                    supplier_gstin=p.supplier_gstin, merchant_gstin=p.merchant_gstin,
                    place_of_supply=("INTRA_STATE (CGST+SGST)" if p.intra_state
                                     else "INTER_STATE (IGST)"))
    rep.notes = [
        "Input tax credit is checked on three independent sources: what the gateway "
        "charged, what the books record as recoverable, and what the GSTR-2B feed "
        "says is claimable. That is two comparisons, not one -- charged vs booked is "
        "the merchant's own posting, booked vs filed is the supplier's. They are "
        "reported separately and never added together: the culprit and the remedy "
        "differ, and one settlement can carry both.",
        f"Supplier and merchant are both in state {p.supplier_state_code}, so the "
        f"supply is {'intra-state and the tax must sit under CGST+SGST' if p.intra_state else 'inter-state and the tax must sit under IGST'} "
        f"(TAX.SUPPLY.place_of_supply@{p.version}).",
        f"An invoice appearing up to {p.claim_window} return period late is credit "
        f"deferred, not credit lost (TAX.CLAIM.window@{p.version}).",
        "The GSTR-2B feed is synthetic, authored for this project. It is not real "
        "filing data and nothing here is tax advice.",
    ]

    if not table_installed(conn):
        rep.installed = False
        rep.totals = {"settlements": 0, "charged_tax_paise": 0, "booked_tax_paise": 0,
                      "claimable_tax_paise": 0, "at_risk_paise": 0, "deferred_paise": 0,
                      "blocked_paise": 0, "matched": 0, "by_status": {}}
        return rep

    # ---- CHARGED: what the settlement report billed --------------------------
    charged = fetch(conn, """
        SELECT s.settlement_id, s.settlement_date,
               s.fee_amount_paise, s.tax_amount_paise
        FROM settlements s
        WHERE s.dataset_id = %s AND s.tax_amount_paise <> 0
        ORDER BY s.settlement_date, s.settlement_id""", (dataset_id,))

    # ---- BOOKED: the merchant's own INPUT_GST postings -----------------------
    booked = {r["settlement_id"]: int(r["t"]) for r in fetch(conn, """
        SELECT settlement_id, COALESCE(SUM(
                 CASE WHEN direction = 'DR' THEN amount_paise ELSE -amount_paise END), 0) AS t
        FROM ledger_entries
        WHERE dataset_id = %s AND account = 'INPUT_GST' AND settlement_id IS NOT NULL
        GROUP BY settlement_id""", (dataset_id,))}

    # ---- CLAIMABLE: the GSTR-2B feed ----------------------------------------
    invoices = {}
    for r in fetch(conn, """
            SELECT invoice_no, invoice_date, return_period, settlement_id,
                   document_type, taxable_value_paise, cgst_paise, sgst_paise,
                   igst_paise, itc_eligible, ineligible_reason, filed_at
            FROM tax_invoices WHERE dataset_id = %s
            ORDER BY invoice_no""", (dataset_id,)):
        if r["settlement_id"]:
            invoices[r["settlement_id"]] = r

    for c in charged:
        rep.lines.append(_match_one(c, booked, invoices, p))

    _roll_up(rep)
    return rep


def _match_one(c, booked, invoices, p: TaxPolicy) -> TaxLine:
    sid = c["settlement_id"]
    period = _period(c["settlement_date"])
    charged_tax = int(c["tax_amount_paise"])
    booked_tax = booked.get(sid, 0)
    inv = invoices.get(sid)

    # Labels are short because they repeat on every block: they identify the
    # source, they do not explain it.
    ev = [{"source": "settlement", "id": sid, "label": "settlement report",
           "amount_paise": charged_tax},
          {"source": "ledger", "id": f"INPUT_GST:{sid}",
           "label": "INPUT_GST postings", "amount_paise": booked_tax}]

    # --- the books leg, evaluated INDEPENDENTLY of the filing leg ------------
    # It never returns early. The merchant's own posting error and the supplier's
    # filing error are different problems with different remedies, and a
    # settlement can have both -- which is exactly what a check that returned
    # here would hide.
    books_delta = booked_tax - charged_tax
    if abs(books_delta) > p.amount_tolerance_paise:
        books_status = BOOKS_MISMATCH
        books_finding = (
            f"INPUT_GST holds {_r(booked_tax)} against {_r(charged_tax)} charged — "
            f"{_r(abs(books_delta))} "
            f"{'over' if books_delta > 0 else 'under'}-recorded.")
    else:
        books_status, books_finding = BOOKS_OK, ""

    books = dict(books_status=books_status, books_delta_paise=books_delta,
                 books_finding=books_finding)

    # --- not filed: the whole credit is at risk ------------------------------
    if inv is None:
        return TaxLine(
            settlement_id=sid, settlement_date=c["settlement_date"].isoformat(),
            return_period=period, charged_fee_paise=int(c["fee_amount_paise"]),
            charged_tax_paise=charged_tax, booked_tax_paise=booked_tax,
            claimable_tax_paise=0, invoice_no=None, invoice_period=None, heads=None,
            status="NOT_FILED", claim_state=CLAIM_STATE["NOT_FILED"],
            at_risk_paise=charged_tax, **books,
            rule=f"TAX.CLAIM.window@{p.version}",
            finding=f"{_r(charged_tax)} charged, nothing filed. Not claimable.",
            evidence=ev + [{"source": "gstr2b", "id": "—",
                            "label": "no line filed", "amount_paise": 0}])

    inv_tax = _inv_tax(inv)
    heads = _heads(inv)
    ev.append({"source": "gstr2b", "id": inv["invoice_no"],
               "label": f"{inv['return_period']} · {heads}", "amount_paise": inv_tax})
    common = dict(settlement_id=sid, settlement_date=c["settlement_date"].isoformat(),
                  return_period=period, charged_fee_paise=int(c["fee_amount_paise"]),
                  charged_tax_paise=charged_tax, booked_tax_paise=booked_tax,
                  claimable_tax_paise=inv_tax, invoice_no=inv["invoice_no"],
                  invoice_period=inv["return_period"], heads=heads, evidence=ev, **books)

    # --- wrong tax heads: right amount, unusable ----------------------------
    if heads != p.expected_heads:
        return TaxLine(**common, status="SPLIT_MISMATCH",
                       claim_state=CLAIM_STATE["SPLIT_MISMATCH"], at_risk_paise=inv_tax,
                       rule=f"TAX.SUPPLY.place_of_supply@{p.version}",
                       finding=f"Filed as {heads}; both parties are in state "
                               f"{p.supplier_state_code}, so this supply is "
                               f"{p.expected_heads}. Right amount, wrong heads — it will "
                               f"not offset.")

    # --- amount disagreement -------------------------------------------------
    if abs(inv_tax - charged_tax) > p.amount_tolerance_paise:
        diff = charged_tax - inv_tax
        return TaxLine(**common, status="AMOUNT_MISMATCH",
                       claim_state=CLAIM_STATE["AMOUNT_MISMATCH"], at_risk_paise=abs(diff),
                       rule=f"TAX.MATCH.amount_tolerance@{p.version}",
                       finding=f"Charged {_r(charged_tax)}, filed {_r(inv_tax)} — "
                               f"{_r(abs(diff))} {'over' if diff > 0 else 'under'} the "
                               f"invoice. Only the filed amount is claimable.")

    # --- the portal says it is not creditable -------------------------------
    if not inv["itc_eligible"]:
        return TaxLine(**common, status="ITC_BLOCKED",
                       claim_state=CLAIM_STATE["ITC_BLOCKED"], at_risk_paise=inv_tax,
                       rule=f"TAX.CLAIM.window@{p.version}",
                       finding=f"Matches on every rupee; GSTR-2B marks it ineligible — "
                               f"{inv['ineligible_reason']}. {_r(inv_tax)} was never "
                               f"claimable.")

    # --- timing --------------------------------------------------------------
    gap = _period_gap(period, inv["return_period"])
    if gap != 0:
        # Late but inside the window is DEFERRED -- real credit, later period.
        # Anything else (too late, or filed before the supply) is AT_RISK.
        within = 0 < gap <= p.claim_window
        return TaxLine(**common, status="PERIOD_MISMATCH",
                       claim_state="DEFERRED" if within else "AT_RISK",
                       at_risk_paise=0 if within else inv_tax,
                       rule=f"TAX.CLAIM.window@{p.version}",
                       finding=(f"Settlement falls in {period} but the invoice appears in "
                                f"{inv['return_period']} — {abs(gap)} period"
                                f"{'s' if abs(gap) != 1 else ''} "
                                f"{'late' if gap > 0 else 'early'}. "
                                + ("Deferred to that period, not lost." if within else
                                   "Outside the claim window.")))

    return TaxLine(**common, status="MATCHED", claim_state=CLAIM_STATE["MATCHED"],
                   at_risk_paise=0, rule=f"TAX.MATCH.amount_tolerance@{p.version}",
                   finding="Charged, booked and filed agree. Fully claimable.")


def _inv_tax(inv) -> int:
    return int(inv["cgst_paise"]) + int(inv["sgst_paise"]) + int(inv["igst_paise"])


def _heads(inv) -> str:
    return "IGST" if int(inv["igst_paise"]) else "CGST+SGST"


def _r(paise: int) -> str:
    """Indian digit grouping, display only. The findings are read on the page
    beside `rupees()` output, so they carry the same symbol."""
    neg, v = paise < 0, abs(int(paise))
    w, f = str(v // 100), f"{v % 100:02d}"
    if len(w) > 3:
        head, tail, parts = w[:-3], w[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        w = ",".join(parts) + "," + tail
    return ("\u2212" if neg else "") + "\u20b9" + w + "." + f


def _roll_up(rep: TaxReport) -> None:
    by_status: dict[str, int] = {}
    for l in rep.lines:
        by_status[l.status] = by_status.get(l.status, 0) + 1

    def total(attr, pred=lambda l: True):
        return sum(getattr(l, attr) for l in rep.lines if pred(l))

    books_bad = [l for l in rep.lines if l.books_status == BOOKS_MISMATCH]
    clean = [l for l in rep.lines
             if l.status == "MATCHED" and l.books_status == BOOKS_OK]

    rep.totals = {
        "settlements": len(rep.lines),
        "charged_tax_paise": total("charged_tax_paise"),
        "booked_tax_paise": total("booked_tax_paise"),
        "claimable_tax_paise": total("claimable_tax_paise"),
        # --- filing leg (booked vs GSTR-2B) ---
        "at_risk_paise": total("at_risk_paise", lambda l: l.claim_state == "AT_RISK"),
        "deferred_paise": total("claimable_tax_paise", lambda l: l.claim_state == "DEFERRED"),
        "blocked_paise": total("claimable_tax_paise", lambda l: l.claim_state == "BLOCKED"),
        "filing_exceptions": len(rep.lines) - by_status.get("MATCHED", 0),
        # --- books leg (charged vs INPUT_GST), reported separately and never
        # added to the figures above: they are different money with different
        # remedies, and one number covering both would say nothing actionable.
        "books_exceptions": len(books_bad),
        "books_over_paise": sum(l.books_delta_paise for l in books_bad
                                if l.books_delta_paise > 0),
        "books_under_paise": -sum(l.books_delta_paise for l in books_bad
                                  if l.books_delta_paise < 0),
        "books_abs_paise": sum(abs(l.books_delta_paise) for l in books_bad),
        # --- both legs clean ---
        "matched": len(clean),
        "match_rate_pct": round(100.0 * len(clean) / len(rep.lines), 2)
                          if rep.lines else 0.0,
        "by_status": by_status,
    }

    # Per return period, because that is the grain a claim is actually filed at.
    periods: dict[str, dict] = {}
    for l in rep.lines:
        d = periods.setdefault(l.return_period, {
            "return_period": l.return_period, "settlements": 0, "charged_tax_paise": 0,
            "booked_tax_paise": 0, "claimable_tax_paise": 0, "at_risk_paise": 0,
            "deferred_paise": 0, "blocked_paise": 0, "books_abs_paise": 0})
        d["settlements"] += 1
        d["charged_tax_paise"] += l.charged_tax_paise
        d["booked_tax_paise"] += l.booked_tax_paise
        d["claimable_tax_paise"] += l.claimable_tax_paise
        if l.books_status == BOOKS_MISMATCH:
            d["books_abs_paise"] += abs(l.books_delta_paise)
        if l.claim_state == "AT_RISK":
            d["at_risk_paise"] += l.at_risk_paise
        elif l.claim_state == "DEFERRED":
            d["deferred_paise"] += l.claimable_tax_paise
        elif l.claim_state == "BLOCKED":
            d["blocked_paise"] += l.claimable_tax_paise
    rep.periods = [periods[k] for k in sorted(periods)]


def to_dict(rep: TaxReport) -> dict:
    order = {s: i for i, s in enumerate(STATUS_ORDER)}
    return {
        "installed": rep.installed,
        "tax_policy_version": rep.tax_policy_version,
        "tax_config_hash": rep.tax_config_hash,
        "supplier_gstin": rep.supplier_gstin,
        "merchant_gstin": rep.merchant_gstin,
        "place_of_supply": rep.place_of_supply,
        "totals": rep.totals,
        "periods": rep.periods,
        # worst first, so the page opens on what needs doing
        # Worst first, so the page opens on what needs doing. A clean filing
        # verdict with broken books still sorts above a fully clean line.
        "lines": [l.__dict__ for l in sorted(
            rep.lines, key=lambda l: (order.get(l.status, 99),
                                      0 if l.books_status == BOOKS_MISMATCH else 1,
                                      -l.at_risk_paise, l.settlement_id))],
        "notes": rep.notes,
        "disclaimer": "Synthetic GSTR-2B feed authored for this project. Not real "
                      "filing data, and not tax advice.",
    }
