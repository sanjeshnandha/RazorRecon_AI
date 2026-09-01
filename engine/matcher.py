"""
Deterministic bank matcher (Delta-2).

Passes run in order. The first pass yielding EXACTLY ONE candidate wins. Two or
more equally-scoring candidates does not "partially succeed" -- the pass fails
and control moves on with both candidates still in play. If nothing later
disambiguates them, the settlement lands in tier C.

There is no numeric confidence threshold anywhere. Ambiguity always beats
confidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from engine.policy import Policy
from engine.subset_sum import find_unique_subset

PASS_TIERS = {
    "EXACT_UTR": "A", "UTR_IN_NARRATION": "A", "UTR_SUFFIX": "A",
    "EXACT_AMOUNT_DATE": "A", "SUBSET_SUM_MERGED": "A", "SUBSET_SUM_SPLIT": "A",
    "AMOUNT_WIDE_WINDOW": "B", "FUZZY_REFERENCE": "B",
}
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def normalise(text: str | None) -> str:
    return _NON_ALNUM.sub("", (text or "").upper())


def token_similarity_bps(a: str, b: str) -> int:
    """Character-bigram Dice coefficient in basis points. Deterministic, no
    library, and it never promotes a match above tier B whatever it returns."""
    a, b = normalise(a), normalise(b)
    if not a or not b:
        return 0
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    return (2 * len(ga & gb) * 10000) // (len(ga) + len(gb))


@dataclass
class MatchResult:
    settlement_id: str
    bank_ids: list[str] = field(default_factory=list)
    pass_name: str | None = None
    tier: str = "C"
    is_ambiguous: bool = False
    matched_paise: int = 0
    note: str = ""


@dataclass
class MatchOutcome:
    results: dict[str, MatchResult]
    candidates: list[dict]                       # every candidate examined, for the audit trail


def run_matcher(snap, policy: Policy) -> MatchOutcome:
    settlements = snap.settlements
    credits = [b for b in snap.bank if b["credit_paise"] > 0]
    results = {s["settlement_id"]: MatchResult(settlement_id=s["settlement_id"]) for s in settlements}
    candidates: list[dict] = []
    claimed_bank: set[str] = set()
    resolved: set[str] = set()

    def record(sid, bid, pass_name, score, selected=False, ambiguous=False):
        candidates.append({"settlement_id": sid, "bank_transaction_id": bid,
                           "pass_name": pass_name, "score_bps": score,
                           "is_selected": selected, "is_ambiguous": ambiguous})

    def select(s, bank_rows, pass_name, note=""):
        sid = s["settlement_id"]
        r = results[sid]
        r.bank_ids = [b["bank_transaction_id"] for b in bank_rows]
        r.pass_name = pass_name
        r.tier = PASS_TIERS[pass_name]
        r.matched_paise = sum(b["credit_paise"] for b in bank_rows)
        r.note = note
        for b in bank_rows:
            claimed_bank.add(b["bank_transaction_id"])
        resolved.add(sid)

    def in_window(b, s, days):
        lo = s["settlement_date"] - timedelta(days=days)
        hi = s["settlement_date"] + timedelta(days=days)
        return lo <= b["transaction_date"] <= hi

    # ---------------- pass 0: EXACT_UTR ----------------------------------
    for s in settlements:
        if not s["settlement_utr"]:
            continue
        hits = [b for b in credits
                if b["settlement_utr"] and b["settlement_utr"] == s["settlement_utr"]
                and b["bank_transaction_id"] not in claimed_bank]
        for b in hits:
            record(s["settlement_id"], b["bank_transaction_id"], "EXACT_UTR", 10000,
                   selected=len(hits) == 1, ambiguous=len(hits) > 1)
        if len(hits) == 1:
            select(s, hits, "EXACT_UTR", "bank line carries the settlement UTR verbatim")
        elif len(hits) > 1:
            results[s["settlement_id"]].is_ambiguous = True

    # ---------------- pass 1: UTR_IN_NARRATION ---------------------------
    for s in settlements:
        if s["settlement_id"] in resolved or not s["settlement_utr"]:
            continue
        utr = normalise(s["settlement_utr"])
        hits = [b for b in credits
                if b["bank_transaction_id"] not in claimed_bank and utr in normalise(b["description"])]
        for b in hits:
            record(s["settlement_id"], b["bank_transaction_id"], "UTR_IN_NARRATION", 10000,
                   selected=len(hits) == 1, ambiguous=len(hits) > 1)
        if len(hits) == 1:
            select(s, hits, "UTR_IN_NARRATION", "full UTR appears in the normalised narration")
        elif len(hits) > 1:
            results[s["settlement_id"]].is_ambiguous = True

    # ---------------- pass 2: UTR_SUFFIX (with the uniqueness guard) -----
    # An 8-character suffix is NOT guaranteed unique -- two unrelated UTRs can
    # coincidentally share a tail. The suffix must be unique across every other
    # settlement UTR inside the date window before this pass may select.
    for s in settlements:
        if s["settlement_id"] in resolved or not s["settlement_utr"]:
            continue
        suffix = normalise(s["settlement_utr"])[-8:]
        rivals = [o for o in settlements
                  if o["settlement_id"] != s["settlement_id"] and o["settlement_utr"]
                  and normalise(o["settlement_utr"])[-8:] == suffix
                  and abs((o["settlement_date"] - s["settlement_date"]).days) <= policy.date_window_days]
        hits = [b for b in credits
                if b["bank_transaction_id"] not in claimed_bank and suffix in normalise(b["description"])]
        if rivals:
            for b in hits:
                record(s["settlement_id"], b["bank_transaction_id"], "UTR_SUFFIX", 5000,
                       selected=False, ambiguous=True)
            results[s["settlement_id"]].note = (
                f"UTR suffix '{suffix}' is shared with "
                f"{', '.join(o['settlement_id'] for o in rivals)} inside the date window -- "
                f"suffix pass refused, deferred to a stronger pass")
            continue
        for b in hits:
            record(s["settlement_id"], b["bank_transaction_id"], "UTR_SUFFIX", 9000,
                   selected=len(hits) == 1, ambiguous=len(hits) > 1)
        if len(hits) == 1:
            select(s, hits, "UTR_SUFFIX", f"UTR suffix '{suffix}' is unique in the window")
        elif len(hits) > 1:
            results[s["settlement_id"]].is_ambiguous = True

    # ---------------- pass 3: EXACT_AMOUNT_DATE (with the rival guard) ---
    # Two settlements can legitimately share a net amount on a date. Matching on
    # amount+date without checking for a sibling silently misassigns money.
    tol = policy.bank_tolerance_days
    for s in settlements:
        sid = s["settlement_id"]
        if sid in resolved:
            continue
        net = s["net_settlement_amount_paise"]
        if net <= 0:
            continue
        lo = s["settlement_date"]
        hi = s["settlement_date"] + timedelta(days=tol)
        rivals = [o for o in settlements
                  if o["settlement_id"] != sid and o["settlement_id"] not in resolved
                  and o["net_settlement_amount_paise"] == net
                  and abs((o["settlement_date"] - s["settlement_date"]).days) <= tol]
        hits = [b for b in credits
                if b["bank_transaction_id"] not in claimed_bank
                and b["credit_paise"] == net and lo <= b["transaction_date"] <= hi]
        if rivals:
            for b in hits:
                record(sid, b["bank_transaction_id"], "EXACT_AMOUNT_DATE", 5000,
                       selected=False, ambiguous=True)
            results[sid].is_ambiguous = True
            results[sid].note = (
                f"another settlement ({', '.join(o['settlement_id'] for o in rivals)}) has the same "
                f"net on the same date -- amount+date is not identity, refusing to select")
            continue
        for b in hits:
            record(sid, b["bank_transaction_id"], "EXACT_AMOUNT_DATE", 9500,
                   selected=len(hits) == 1, ambiguous=len(hits) > 1)
        if len(hits) == 1:
            select(s, hits, "EXACT_AMOUNT_DATE", "exact net, inside the bank tolerance window, "
                                                 "and no competing settlement with the same figure")
        elif len(hits) > 1:
            results[sid].is_ambiguous = True
            results[sid].note = f"{len(hits)} bank credits match this net and date equally well"

    # ---------------- pass 4: SUBSET_SUM ---------------------------------
    # MERGED: one credit covers N settlements.
    for b in credits:
        if b["bank_transaction_id"] in claimed_bank:
            continue
        pool = [(s["settlement_id"], s["net_settlement_amount_paise"]) for s in settlements
                if s["settlement_id"] not in resolved and s["net_settlement_amount_paise"] > 0
                and in_window(b, s, policy.date_window_days)]
        subset, n = find_unique_subset(pool, b["credit_paise"],
                                       policy.subset_sum_max_candidates,
                                       policy.subset_sum_max_subset_size)
        if subset:
            for sid in subset:
                s = snap.settlement_by_id[sid]
                record(sid, b["bank_transaction_id"], "SUBSET_SUM_MERGED", 10000, selected=True)
                results[sid].bank_ids = [b["bank_transaction_id"]]
                results[sid].pass_name = "SUBSET_SUM_MERGED"
                results[sid].tier = "A"
                results[sid].matched_paise = s["net_settlement_amount_paise"]
                results[sid].note = (
                    f"one credit of {b['credit_paise']} paise covers {len(subset)} settlements "
                    f"({', '.join(subset)}); exactly one subset within the bound sums to it")
                resolved.add(sid)
            claimed_bank.add(b["bank_transaction_id"])
        elif n > 1:
            for sid, _ in pool:
                record(sid, b["bank_transaction_id"], "SUBSET_SUM_MERGED", 5000, ambiguous=True)

    # SPLIT: one settlement arrives as N credits.
    for s in settlements:
        sid = s["settlement_id"]
        if sid in resolved or s["net_settlement_amount_paise"] <= 0:
            continue
        pool = [(b["bank_transaction_id"], b["credit_paise"]) for b in credits
                if b["bank_transaction_id"] not in claimed_bank and in_window(b, s, policy.date_window_days)]
        subset, n = find_unique_subset(pool, s["net_settlement_amount_paise"],
                                       policy.subset_sum_max_candidates,
                                       policy.subset_sum_max_subset_size)
        if subset:
            rows = [snap.bank_by_id[i] for i in subset]
            for b in rows:
                record(sid, b["bank_transaction_id"], "SUBSET_SUM_SPLIT", 10000, selected=True)
            select(s, rows, "SUBSET_SUM_SPLIT",
                   f"settlement arrived as {len(rows)} credits; exactly one subset within the "
                   f"bound sums to the net")
        elif n > 1:
            results[sid].is_ambiguous = True
            for bid, _ in pool:
                record(sid, bid, "SUBSET_SUM_SPLIT", 5000, ambiguous=True)

    # ---------------- pass 5: AMOUNT_WIDE_WINDOW (tier B) ----------------
    for s in settlements:
        sid = s["settlement_id"]
        if sid in resolved or s["net_settlement_amount_paise"] <= 0:
            continue
        net = s["net_settlement_amount_paise"]
        hits = [b for b in credits
                if b["bank_transaction_id"] not in claimed_bank and b["credit_paise"] == net
                and in_window(b, s, policy.date_window_days)]
        rivals = [o for o in settlements
                  if o["settlement_id"] != sid and o["settlement_id"] not in resolved
                  and o["net_settlement_amount_paise"] == net]
        for b in hits:
            record(sid, b["bank_transaction_id"], "AMOUNT_WIDE_WINDOW", 8000,
                   selected=len(hits) == 1 and not rivals, ambiguous=len(hits) > 1 or bool(rivals))
        if len(hits) == 1 and not rivals:
            select(s, hits, "AMOUNT_WIDE_WINDOW",
                   "exact net inside the wide date window, no UTR evidence -- amount alone is not "
                   "identity, so this is tier B and goes to a human")
        elif hits:
            results[sid].is_ambiguous = True

    # ---------------- pass 6: FUZZY_REFERENCE (tier B) -------------------
    # Similarity alone is not identity. This pass requires exact amount AND date
    # proximity AND exactly one candidate over the threshold, and it can never
    # promote above tier B however high the score.
    for s in settlements:
        sid = s["settlement_id"]
        if sid in resolved or not s["settlement_utr"] or s["net_settlement_amount_paise"] <= 0:
            continue
        net = s["net_settlement_amount_paise"]
        scored = []
        for b in credits:
            if b["bank_transaction_id"] in claimed_bank or b["credit_paise"] != net:
                continue
            if not in_window(b, s, policy.date_window_days):
                continue
            score = token_similarity_bps(s["settlement_utr"], b["description"])
            if score >= policy.fuzzy_reference_min_score_bps:
                scored.append((b, score))
        for b, score in scored:
            record(sid, b["bank_transaction_id"], "FUZZY_REFERENCE", score,
                   selected=len(scored) == 1, ambiguous=len(scored) > 1)
        if len(scored) == 1:
            select(s, [scored[0][0]], "FUZZY_REFERENCE",
                   f"normalised similarity {scored[0][1]}bps with exact amount and date proximity; "
                   f"fuzzy evidence never promotes above tier B")
        elif len(scored) > 1:
            results[sid].is_ambiguous = True

    return MatchOutcome(results=results, candidates=candidates)
