"""
Anomaly mutation pass (spec step 12).

Runs over CLEAN data. Every planting function writes its ground_truth_anomalies
row AT PLANT TIME, computed from the values it just wrote -- never reconstructed
afterwards by re-deriving from the mutated data, which would risk the generator
and the evaluator sharing the same bug.

Each settlement receives at most one Delta-1/Delta-2 anomaly, so a diagnosis is
never ambiguous about which planted defect it found. Delta-3 and Delta-4
anomalies may sit on settlements that are otherwise clean -- that is deliberate:
a settlement can reconcile perfectly while its ledger is double-posted or a
seller is underpaid, which is the whole argument for reporting them separately.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from engine.money import bps
from engine.policy import Policy
from generator.calendar import add_working_days

DRIFTED_MDR_BPS = 250


class Planter:
    def __init__(self, ds, rng: random.Random):
        self.ds = ds
        self.rng = rng
        self.policy: Policy = ds.policy
        # Ground-truth ids continue across batches so an appended anomaly never
        # overwrites one planted in an earlier cycle.
        self.n = ds._seq.get("gt", 0)
        self.claimed_d12: set[str] = set()   # settlements holding a D1/D2 anomaly
        self.claimed_d3: set[str] = set()
        self.claimed_alloc: set[str] = set()
        ds.index()

    # ------------------------------------------------------------------ util
    def gt(self, anomaly_type, subject_type, subject_id, settlement_id, delta_kind,
           exception_type, planted_amount, is_resolvable, notes,
           original_field=None, original_value=None, mutated_value=None):
        self.n += 1
        self.ds.ground_truth.append({
            "anomaly_id": f"GT_{self.n:04d}", "anomaly_type": anomaly_type,
            "subject_type": subject_type, "subject_id": subject_id,
            "settlement_id": settlement_id, "expected_delta_kind": delta_kind,
            "expected_exception_type": exception_type, "original_field": original_field,
            "original_value_paise": original_value, "mutated_value_paise": mutated_value,
            "planted_amount_paise": planted_amount, "is_resolvable": is_resolvable,
            "notes": notes})

    def items(self, sid: str) -> list[dict]:
        return [i for i in self.ds.settlement_items if i["settlement_id"] == sid]

    def rebuild_header(self, s: dict) -> None:
        """Recompute the header rollup from items, so the ONLY defect in this
        settlement is the one we planted -- not an incidental rollup mismatch."""
        its = self.items(s["settlement_id"])
        s["gross_amount_paise"] = sum(i["amount_paise"] for i in its if i["transaction_type"] == "PAYMENT")
        s["refund_amount_paise"] = sum(-i["amount_paise"] for i in its if i["transaction_type"] == "REFUND")
        s["fee_amount_paise"] = sum(i["fee_paise"] for i in its if i["transaction_type"] != "TRANSFER")
        s["tax_amount_paise"] = sum(i["tax_paise"] for i in its if i["transaction_type"] != "TRANSFER")
        s["adjustment_amount_paise"] = sum(i["amount_paise"] for i in its if i["transaction_type"] == "ADJUSTMENT")
        s["net_settlement_amount_paise"] = (s["gross_amount_paise"] - s["refund_amount_paise"]
                                            - s["fee_amount_paise"] - s["tax_amount_paise"]
                                            + s["adjustment_amount_paise"])

    def sync_bank(self, s: dict) -> None:
        """Keep the settlement's own bank credit equal to its net, so a Delta-1
        mutation does not accidentally manufacture a Delta-2 as well."""
        for b in self.ds.bank_transactions:
            if b.get("_settlement_id") == s["settlement_id"] and b["credit_paise"] > 0:
                b["credit_paise"] = s["net_settlement_amount_paise"]

    def pick_settlements(self, k: int, pred=None, claim="d12") -> list[dict]:
        claimed = getattr(self, f"claimed_{claim}")
        pool = [s for s in self.ds.settlements
                if s["settlement_id"] not in self.claimed_d12
                and s["settlement_id"] not in claimed
                and (pred is None or pred(s))]
        self.rng.shuffle(pool)
        chosen = pool[:k]
        for s in chosen:
            claimed.add(s["settlement_id"])
            if claim == "d12":
                self.claimed_d12.add(s["settlement_id"])
        return chosen

    def payment_items(self, sid: str, method: str | None = None) -> list[dict]:
        out = []
        for i in self.items(sid):
            if i["transaction_type"] != "PAYMENT":
                continue
            p = self.ds.by_payment[i["payment_id"]]
            if method is None or p["payment_method"] == method:
                out.append((i, p))
        return out

    def new_item(self, sid: str, **kw) -> dict:
        it = {"settlement_item_id": self.ds.next_id("si", "SI_", 6), "settlement_id": sid,
              "transaction_type": kw["transaction_type"], "payment_id": kw.get("payment_id"),
              "refund_id": kw.get("refund_id"), "adjustment_id": kw.get("adjustment_id"),
              "transfer_id": kw.get("transfer_id"), "amount_paise": kw["amount_paise"],
              "fee_paise": kw.get("fee_paise", 0), "tax_paise": kw.get("tax_paise", 0),
              "transaction_date": kw["transaction_date"]}
        self.ds.settlement_items.append(it)
        return it

    # ================================================================ DELTA 1
    def d1_fee_rate_drift(self, count: int) -> None:
        """Fee charged at 250bps instead of POLICY.MDR.CARD@1.0.0 (200bps).
        The gateway also taxes its own inflated fee, so the planted impact is
        the fee excess PLUS the GST consequence of that excess."""
        done = 0
        for s in self.pick_settlements(count * 6):
            if done >= count:
                self.claimed_d12.discard(s["settlement_id"]); continue
            cands = self.payment_items(s["settlement_id"], "CARD")
            if not cands:
                self.claimed_d12.discard(s["settlement_id"]); continue
            it, p = max(cands, key=lambda x: x[1]["amount_paise"])
            policy_fee, policy_tax = it["fee_paise"], it["tax_paise"]
            drift_fee = bps(p["amount_paise"], DRIFTED_MDR_BPS)
            drift_tax = bps(drift_fee, self.policy.gst_on_fee_bps)
            it["fee_paise"], it["tax_paise"] = drift_fee, drift_tax
            self.rebuild_header(s); self.sync_bank(s)
            impact = (drift_fee - policy_fee) + (drift_tax - policy_tax)
            self.gt("D1_FEE_RATE_DRIFT", "payment", p["payment_id"], s["settlement_id"],
                    "D1_COMPUTE", "FEE_RATE_MISMATCH", impact, True,
                    f"Charged at {DRIFTED_MDR_BPS}bps instead of policy MDR.CARD@{self.policy.version} "
                    f"(200bps): fee {policy_fee}->{drift_fee} paise, GST {policy_tax}->{drift_tax} paise",
                    original_field="mdr_bps", original_value=policy_fee, mutated_value=drift_fee)
            done += 1

    def d1_tax_aggregate_rounding(self, count: int) -> None:
        """GST computed on the AGGREGATED fee instead of PER_ITEM. Sub-rupee
        drift that still has to reconcile to zero."""
        done = 0
        for s in self.pick_settlements(count * 12):
            if done >= count:
                self.claimed_d12.discard(s["settlement_id"]); continue
            its = [i for i in self.items(s["settlement_id"])
                   if i["transaction_type"] == "PAYMENT" and i["fee_paise"] > 0]
            if len(its) < 2:
                self.claimed_d12.discard(s["settlement_id"]); continue
            per_item = sum(i["tax_paise"] for i in its)
            aggregate = bps(sum(i["fee_paise"] for i in its), self.policy.gst_on_fee_bps)
            diff = aggregate - per_item
            if diff == 0:
                self.claimed_d12.discard(s["settlement_id"]); continue
            target = max(its, key=lambda i: i["fee_paise"])
            before = target["tax_paise"]
            target["tax_paise"] = before + diff
            self.rebuild_header(s); self.sync_bank(s)
            self.gt("D1_TAX_AGGREGATE_ROUNDING", "settlement", s["settlement_id"], s["settlement_id"],
                    "D1_COMPUTE", "TAX_ROUNDING_MISMATCH", abs(diff), True,
                    f"GST charged on aggregated fee ({aggregate} paise) instead of PER_ITEM "
                    f"({per_item} paise) per POLICY.TAX.GST_ON_FEE@{self.policy.version}",
                    original_field="tax_paise", original_value=before, mutated_value=before + diff)
            done += 1

    def _detach_refund_item(self, s: dict, refund_id: str) -> int | None:
        for idx, i in enumerate(self.items(s["settlement_id"])):
            if i["transaction_type"] == "REFUND" and i["refund_id"] == refund_id:
                amt = -i["amount_paise"]
                self.ds.settlement_items.remove(i)
                return amt
        return None

    def d1_refund_not_deducted(self, count: int) -> None:
        """An in-period PROCESSED refund is absent from settlement_items, so it
        was never taken off. The merchant is OVERPAID by exactly that amount."""
        done = 0
        for s in self.pick_settlements(count * 8):
            if done >= count:
                self.claimed_d12.discard(s["settlement_id"]); continue
            refs = [i for i in self.items(s["settlement_id"]) if i["transaction_type"] == "REFUND"]
            if not refs:
                self.claimed_d12.discard(s["settlement_id"]); continue
            victim = max(refs, key=lambda i: -i["amount_paise"])
            rid = victim["refund_id"]
            amt = self._detach_refund_item(s, rid)
            self.rebuild_header(s); self.sync_bank(s)
            self.gt("D1_REFUND_NOT_DEDUCTED", "refund", rid, s["settlement_id"],
                    "D1_COMPUTE", "REFUND_NOT_DEDUCTED", amt, True,
                    f"Refund {rid} is PROCESSED and dated inside "
                    f"[{s['settlement_period_start']}, {s['settlement_period_end']}] but has no "
                    f"REFUND item; merchant overpaid by {amt} paise",
                    original_field="settlement_item.amount_paise", original_value=-amt, mutated_value=0)
            done += 1

    def d1_refund_partial_multi(self, count: int) -> None:
        """Two partial refunds against ONE payment, both in period, only one
        deducted. Exercises the 'refunds is a sum, not a lookup' rule."""
        done = 0
        for s in self.pick_settlements(count * 20):
            if done >= count:
                self.claimed_d12.discard(s["settlement_id"]); continue
            pay_items = self.payment_items(s["settlement_id"])
            cand = None
            for it, p in pay_items:
                if p["amount_paise"] > 400000 and not any(
                        r["payment_id"] == p["payment_id"] for r in self.ds.refunds):
                    cand = p; break
            if cand is None:
                self.claimed_d12.discard(s["settlement_id"]); continue
            day = s["settlement_period_end"]
            a1 = bps(cand["amount_paise"], 2000)
            a2 = bps(cand["amount_paise"], 1500)
            new = []
            for amt in (a1, a2):
                rid = self.ds.next_id("refund", "R_")
                self.ds.refunds.append({"refund_id": rid, "payment_id": cand["payment_id"],
                                        "refund_amount_paise": amt, "refund_status": "PROCESSED",
                                        "refund_date": day,
                                        "refund_reason": "Partial refund (planted, multi-refund case)"})
                new.append((rid, amt))
            # only the FIRST of the two is itemised
            self.new_item(s["settlement_id"], transaction_type="REFUND", refund_id=new[0][0],
                          amount_paise=-new[0][1], transaction_date=day)
            self.rebuild_header(s); self.sync_bank(s)
            self.ds.index()
            self.gt("D1_REFUND_PARTIAL_MULTI", "refund", new[1][0], s["settlement_id"],
                    "D1_COMPUTE", "REFUND_NOT_DEDUCTED", new[1][1], True,
                    f"Payment {cand['payment_id']} has two in-period partial refunds "
                    f"({new[0][0]}={new[0][1]}, {new[1][0]}={new[1][1]} paise); only the first was "
                    f"deducted. An engine that stops after one attribution leaves a residual.",
                    original_field="settlement_item.amount_paise",
                    original_value=-new[1][1], mutated_value=0)
            done += 1

    def d1_refund_outside_period(self, count: int) -> None:
        """FALSE-POSITIVE TRAP. A refund processed AFTER period_end but BEFORE
        settlement_date. It belongs to a later settlement. The correct engine
        behaviour is to leave this settlement completely alone."""
        done = 0
        for s in self.pick_settlements(count * 8):
            if done >= count:
                self.claimed_d12.discard(s["settlement_id"]); continue
            pay_items = self.payment_items(s["settlement_id"])
            if not pay_items:
                self.claimed_d12.discard(s["settlement_id"]); continue
            after = s["settlement_period_end"] + timedelta(days=1)
            if after >= s["settlement_date"]:
                self.claimed_d12.discard(s["settlement_id"]); continue
            free = [(it, p) for it, p in pay_items
                    if not any(r["payment_id"] == p["payment_id"] for r in self.ds.refunds)]
            if not free:
                self.claimed_d12.discard(s["settlement_id"]); continue
            # the refund must be correctly handled by the settlement whose period
            # DOES contain it, otherwise the trap would plant a second, real
            # anomaly there and the test would be measuring the wrong thing
            later = next((x for x in self.ds.settlements
                          if x["settlement_period_start"] <= after <= x["settlement_period_end"]
                          and x["settlement_id"] not in self.claimed_d12), None)
            if later is None:
                self.claimed_d12.discard(s["settlement_id"]); continue
            it, p = max(free, key=lambda x: x[1]["amount_paise"])
            amt = bps(p["amount_paise"], 4000)
            rid = self.ds.next_id("refund", "R_")
            self.ds.refunds.append({"refund_id": rid, "payment_id": p["payment_id"],
                                    "refund_amount_paise": amt, "refund_status": "PROCESSED",
                                    "refund_date": after,
                                    "refund_reason": "Refund after period close (planted trap)"})
            self.new_item(later["settlement_id"], transaction_type="REFUND", refund_id=rid,
                          amount_paise=-amt, transaction_date=after)
            self.rebuild_header(later); self.sync_bank(later)
            self.ds.index()
            self.gt("D1_REFUND_OUTSIDE_PERIOD", "refund", rid, s["settlement_id"],
                    "D1_COMPUTE", "NONE", amt, True,
                    f"Refund {rid} dated {after} is AFTER period_end {s['settlement_period_end']} "
                    f"but BEFORE settlement_date {s['settlement_date']}. It belongs to a later "
                    f"settlement -- {later['settlement_id']}, where it IS correctly itemised. "
                    f"Attributing {amt} paise to {s['settlement_id']} would be a fabricated "
                    f"explanation. Correct behaviour: no exception on either settlement.",
                    original_field="settlement_id", original_value=None, mutated_value=None)
            self.gt("D1_REFUND_OUTSIDE_PERIOD", "settlement", later["settlement_id"],
                    later["settlement_id"], "D1_COMPUTE", "NONE", amt, True,
                    f"Receiving end of the period-gate trap: refund {rid} belongs HERE and is "
                    f"itemised here, so this settlement must also stay clean.")
            done += 1

    def d1_header_rollup_mismatch(self, count: int) -> None:
        for s in self.pick_settlements(count):
            shortfall = self.rng.randrange(50000, 900000, 100)
            before = s["gross_amount_paise"]
            s["gross_amount_paise"] = before - shortfall
            s["net_settlement_amount_paise"] -= shortfall
            self.sync_bank(s)
            self.gt("D1_HEADER_ROLLUP_MISMATCH", "settlement", s["settlement_id"], s["settlement_id"],
                    "D1_COMPUTE", "HEADER_ROLLUP_MISMATCH", shortfall, True,
                    f"Header gross claims {before - shortfall} paise while settlement_items sum to "
                    f"{before}. settlement_items is the source of truth.",
                    original_field="gross_amount_paise", original_value=before,
                    mutated_value=before - shortfall)

    def d1_adjustment_applied(self, count: int) -> None:
        """A chargeback really was deducted from the payout, but the gateway
        never itemised it. The adjustments table is the evidence."""
        for s in self.pick_settlements(count):
            amount = self.rng.randrange(30000, 600000, 100)
            aid = self.ds.next_id("adj", "ADJ_")
            self.ds.adjustments.append({
                "adjustment_id": aid, "settlement_id": s["settlement_id"],
                "adjustment_type": "CHARGEBACK", "amount_paise": -amount,
                "reason": f"Chargeback raised against {s['settlement_id']} (planted)",
                "created_at": self.ds.by_settlement[s["settlement_id"]]["settlement_date"],
                "status": "APPLIED", "ref_payment_id": None})
            s["net_settlement_amount_paise"] -= amount
            self.sync_bank(s)
            self.gt("D1_ADJUSTMENT_APPLIED", "adjustment", aid, s["settlement_id"],
                    "D1_COMPUTE", "ADJUSTMENT_UNEXPLAINED", amount, True,
                    f"CHARGEBACK {aid} for -{amount} paise was deducted from the payout but never "
                    f"appears in settlement_items. It is discoverable in the adjustments table.",
                    original_field="net_settlement_amount_paise",
                    original_value=s["net_settlement_amount_paise"] + amount,
                    mutated_value=s["net_settlement_amount_paise"])

    def d1_fee_corrected_by_adjustment(self, count: int) -> None:
        """DOUBLE-COUNT TRAP. A fee is over-charged AND a FEE_CORRECTION
        adjustment has already given part of it back. Only the residual fee
        error may be attributed to ATTR.FEE_RATE."""
        done = 0
        for s in self.pick_settlements(count * 10):
            if done >= count:
                self.claimed_d12.discard(s["settlement_id"]); continue
            cands = self.payment_items(s["settlement_id"], "CARD")
            if not cands:
                self.claimed_d12.discard(s["settlement_id"]); continue
            it, p = max(cands, key=lambda x: x[1]["amount_paise"])
            policy_fee, policy_tax = it["fee_paise"], it["tax_paise"]
            drift_fee = bps(p["amount_paise"], DRIFTED_MDR_BPS)
            drift_tax = bps(drift_fee, self.policy.gst_on_fee_bps)
            drift = (drift_fee - policy_fee) + (drift_tax - policy_tax)
            corrected = drift // 2
            if corrected <= 0:
                self.claimed_d12.discard(s["settlement_id"]); continue
            it["fee_paise"], it["tax_paise"] = drift_fee, drift_tax
            self.rebuild_header(s)
            aid = self.ds.next_id("adj", "ADJ_")
            self.ds.adjustments.append({
                "adjustment_id": aid, "settlement_id": s["settlement_id"],
                "adjustment_type": "FEE_CORRECTION", "amount_paise": corrected,
                "reason": f"Partial fee correction for {p['payment_id']} (planted)",
                "created_at": s["settlement_date"], "status": "APPLIED",
                "ref_payment_id": p["payment_id"]})
            s["net_settlement_amount_paise"] += corrected     # money really was credited back
            self.sync_bank(s)
            self.gt("D1_FEE_CORRECTED_BY_ADJUSTMENT", "payment", p["payment_id"], s["settlement_id"],
                    "D1_COMPUTE", "FEE_RATE_MISMATCH", drift - corrected, True,
                    f"Fee drift of {drift} paise on {p['payment_id']}, of which FEE_CORRECTION {aid} "
                    f"already returned {corrected} paise. Only the residual {drift - corrected} paise "
                    f"may be attributed to ATTR.FEE_RATE -- attributing the full drift double-counts.",
                    original_field="mdr_bps", original_value=policy_fee, mutated_value=drift_fee)
            done += 1

    # ================================================================ DELTA 2
    def _bank_for(self, sid: str) -> dict | None:
        for b in self.ds.bank_transactions:
            if b.get("_settlement_id") == sid and b["credit_paise"] > 0:
                return b
        return None

    def d2_timing_next_day(self, count: int) -> None:
        for s in self.pick_settlements(count):
            b = self._bank_for(s["settlement_id"])
            if not b:
                continue
            before = b["transaction_date"]
            b["transaction_date"] = add_working_days(s["settlement_date"], 1, self.policy)
            lag = (b["transaction_date"] - before).days
            self.gt("D2_TIMING_NEXT_DAY", "bank_transaction", b["bank_transaction_id"],
                    s["settlement_id"], "D2_BANK", "TIMING_DIFFERENCE", 0, True,
                    f"Credit landed {b['transaction_date']} instead of {before} -- one business day "
                    f"late, inside POLICY.BANK.tolerance_days@{self.policy.version} = "
                    f"{self.policy.bank_tolerance_days}. No money is missing; the engine must say so "
                    f"rather than raising a shortfall. (calendar slip {lag}d)")

    def d2_narration_no_utr(self, count: int) -> None:
        """Description carries no UTR at all. Half land inside the tight
        tolerance window (pass EXACT_AMOUNT_DATE, tier A); the rest land outside
        it but inside date_window_days, so they can only be reached by
        AMOUNT_WIDE_WINDOW -- which is tier B by design."""
        chosen = self.pick_settlements(count)
        for k, s in enumerate(chosen):
            b = self._bank_for(s["settlement_id"])
            if not b:
                continue
            wide = k >= count // 2
            b["settlement_utr"] = None
            b["description"] = "NEFT-RAZORPAYSOFTWAREPVTLTD-SETTLEMENT"
            if wide:
                b["transaction_date"] = s["settlement_date"] + timedelta(
                    days=self.policy.bank_tolerance_days + 2)
            self.gt("D2_NARRATION_NO_UTR", "bank_transaction", b["bank_transaction_id"],
                    s["settlement_id"], "D2_BANK",
                    "NONE" if not wide else "UTR_MISSING", 0, True,
                    ("No UTR on the bank line and none in the narration. Resolvable on exact "
                     "amount + date inside tolerance -> tier A." if not wide else
                     "No UTR on the bank line and the credit sits outside the tolerance window, so "
                     "only the wide-window amount pass can reach it -> tier B, human review. "
                     "String similarity never promotes to tier A."))

    def d2_merged_credit(self, count: int) -> None:
        """One bank credit covers two settlements -- subset-sum territory."""
        for _ in range(count):
            first = self.pick_settlements(1, pred=lambda s: s["net_settlement_amount_paise"] > 0)
            if not first:
                return
            a = first[0]
            near = self.pick_settlements(
                1, pred=lambda s: s["net_settlement_amount_paise"] > 0
                and 0 < abs((s["settlement_date"] - a["settlement_date"]).days)
                <= self.policy.date_window_days)
            if not near:
                return
            b = near[0]
            ba, bb = self._bank_for(a["settlement_id"]), self._bank_for(b["settlement_id"])
            if not ba or not bb:
                continue
            total = ba["credit_paise"] + bb["credit_paise"]
            ba["credit_paise"] = total
            ba["settlement_utr"] = None
            ba["description"] = "NEFT-RAZORPAYSOFTWAREPVTLTD-BULK SETTLEMENT"
            ba["transaction_date"] = max(a["settlement_date"], b["settlement_date"])
            ba["_settlement_id"] = None
            self.ds.bank_transactions.remove(bb)
            self.gt("D2_MERGED_CREDIT", "bank_transaction", ba["bank_transaction_id"],
                    a["settlement_id"], "D2_BANK", "MERGED_BANK_CREDIT", total, True,
                    f"One credit of {total} paise covers {a['settlement_id']} and "
                    f"{b['settlement_id']}, with no UTR to split it. Bounded subset-sum resolves it "
                    f"deterministically -- no model required.")
            self.gt("D2_MERGED_CREDIT", "bank_transaction", ba["bank_transaction_id"],
                    b["settlement_id"], "D2_BANK", "MERGED_BANK_CREDIT", total, True,
                    f"Second leg of the merged credit {ba['bank_transaction_id']}.")

    def d2_split_credit(self, count: int) -> None:
        for s in self.pick_settlements(count, pred=lambda s: s["net_settlement_amount_paise"] > 20000):
            b = self._bank_for(s["settlement_id"])
            if not b:
                continue
            total = b["credit_paise"]
            first = total // 3
            b["credit_paise"] = first
            b["settlement_utr"] = None
            b["description"] = "NEFT-RAZORPAYSOFTWAREPVTLTD-SETTLEMENT PART 1"
            b["_settlement_id"] = None
            bid = self.ds.next_id("bank", "B_")
            self.ds.bank_transactions.append({
                "bank_transaction_id": bid, "transaction_date": b["transaction_date"],
                "description": "NEFT-RAZORPAYSOFTWAREPVTLTD-SETTLEMENT PART 2",
                "credit_paise": total - first, "debit_paise": 0,
                "bank_reference": f"BREF{bid}", "settlement_utr": None, "_settlement_id": None})
            self.gt("D2_SPLIT_CREDIT", "settlement", s["settlement_id"], s["settlement_id"],
                    "D2_BANK", "SPLIT_BANK_CREDIT", total, True,
                    f"Settlement arrived as two credits ({first} + {total - first} paise), neither "
                    f"carrying a UTR. Subset-sum over unmatched credits resolves it.")

    def d2_settlement_on_hold(self, count: int) -> None:
        for s in self.pick_settlements(count):
            b = self._bank_for(s["settlement_id"])
            if b:
                self.ds.bank_transactions.remove(b)
            before_utr = s["settlement_utr"]
            s["settlement_status"] = "ON_HOLD"
            s["settlement_utr"] = None
            self.gt("D2_SETTLEMENT_ON_HOLD", "settlement", s["settlement_id"], s["settlement_id"],
                    "D2_BANK", "MISSING_BANK_CREDIT", s["net_settlement_amount_paise"], True,
                    f"Settlement is ON_HOLD: no UTR (was {before_utr}) and no bank credit. The full "
                    f"net of {s['net_settlement_amount_paise']} paise is missing from the bank, and "
                    f"the status explains all of it.",
                    original_field="settlement_status", original_value=None, mutated_value=None)

    def d2_suffix_collision(self, count: int) -> None:
        """MATCHER GUARD. Two unrelated UTRs share the same last 8 characters in
        the same window. A naive UTR_SUFFIX pass mismatches; a correct one
        refuses and lets a stronger pass decide."""
        for _ in range(count):
            pair = self.pick_settlements(2)
            if len(pair) < 2:
                return
            a, b = pair
            if a["settlement_date"] < b["settlement_period_end"]:
                a, b = b, a          # keep settlement_date >= period_end on both
            if a["settlement_date"] < b["settlement_period_end"]:
                continue
            ba, bb = self._bank_for(a["settlement_id"]), self._bank_for(b["settlement_id"])
            if not ba or not bb:
                continue
            # force b's UTR to end in a's last 8 chars
            new_utr = b["settlement_utr"][:-8] + a["settlement_utr"][-8:]
            b["settlement_utr"] = new_utr
            bb["settlement_utr"] = new_utr
            b["settlement_date"] = a["settlement_date"]
            bb["transaction_date"] = ba["transaction_date"]
            for x, bx in ((a, ba), (b, bb)):
                bx["settlement_utr"] = None
                bx["description"] = f"NEFT CR RZRPAY {x['settlement_utr'][-8:]}"
            self.gt("D2_SUFFIX_COLLISION", "settlement", a["settlement_id"], a["settlement_id"],
                    "D2_BANK", "NONE", 0, True,
                    f"{a['settlement_id']} and {b['settlement_id']} carry UTRs sharing the suffix "
                    f"'{a['settlement_utr'][-8:]}' in the same window, and both bank lines quote only "
                    f"the suffix. UTR_SUFFIX must refuse to select; a later pass (exact amount + "
                    f"date) must be what resolves it. A coin-flip match here is a false auto-match.")

    def d2_same_amount_same_day(self, count: int) -> None:
        """AMBIGUITY GUARD. Two settlements with identical net on identical
        dates, and neither bank line carries a UTR. Amount+date is not identity."""
        for _ in range(count):
            pair = self.pick_settlements(2, pred=lambda s: s["net_settlement_amount_paise"] > 100000)
            if len(pair) < 2:
                return
            a, b = pair
            if a["settlement_date"] < b["settlement_period_end"]:
                a, b = b, a
            if a["settlement_date"] < b["settlement_period_end"]:
                continue
            ba, bb = self._bank_for(a["settlement_id"]), self._bank_for(b["settlement_id"])
            if not ba or not bb:
                continue
            b["net_settlement_amount_paise"] = a["net_settlement_amount_paise"]
            b["settlement_date"] = a["settlement_date"]
            # the header must stay self-consistent: absorb the change as an adjustment
            delta = b["net_settlement_amount_paise"] - (
                b["gross_amount_paise"] - b["refund_amount_paise"] - b["fee_amount_paise"]
                - b["tax_amount_paise"] + b["adjustment_amount_paise"])
            if delta:
                aid = self.ds.next_id("adj", "ADJ_")
                self.ds.adjustments.append({
                    "adjustment_id": aid, "settlement_id": b["settlement_id"],
                    "adjustment_type": "MANUAL", "amount_paise": delta,
                    "reason": "Manual balancing entry (planted, same-amount scenario)",
                    "created_at": b["settlement_date"], "status": "APPLIED", "ref_payment_id": None})
                self.new_item(b["settlement_id"], transaction_type="ADJUSTMENT", adjustment_id=aid,
                              amount_paise=delta, transaction_date=b["settlement_date"])
                b["adjustment_amount_paise"] += delta
            for x, bx in ((a, ba), (b, bb)):
                bx["credit_paise"] = x["net_settlement_amount_paise"]
                bx["transaction_date"] = x["settlement_date"]
                bx["settlement_utr"] = None
                bx["description"] = "NEFT-RAZORPAYSOFTWAREPVTLTD-SETTLEMENT"
            for x in (a, b):
                self.gt("D2_SAME_AMOUNT_SAME_DAY", "settlement", x["settlement_id"], x["settlement_id"],
                        "D2_BANK", "AMBIGUOUS_BANK_MATCH", x["net_settlement_amount_paise"], True,
                        f"{a['settlement_id']} and {b['settlement_id']} have identical net "
                        f"({a['net_settlement_amount_paise']} paise) on {a['settlement_date']}, and "
                        f"neither credit carries a UTR. Auto-matching on amount+date would assign one "
                        f"settlement's money to the other. Tier C is the correct answer.")

    # ================================================================ DELTA 3
    def _settlement_groups(self, sid: str) -> list[str]:
        return sorted({le["entry_group_id"] for le in self.ds.ledger_entries
                       if le["settlement_id"] == sid and le["description"].startswith("settlement")})

    def d3_duplicate_ledger(self, count: int) -> None:
        done = 0
        for s in self.pick_settlements(count * 4, claim="d3"):
            if done >= count:
                self.claimed_d3.discard(s["settlement_id"]); continue
            groups = self._settlement_groups(s["settlement_id"])
            if not groups:
                self.claimed_d3.discard(s["settlement_id"]); continue
            g = groups[len(groups) // 2]
            legs = [le for le in self.ds.ledger_entries if le["entry_group_id"] == g]
            self.ds._grp_seq += 1
            newg = f"G_{self.ds._grp_seq:06d}_DUP"
            clearing = 0
            for le in legs:
                copy = dict(le)
                copy["ledger_entry_id"] = self.ds.next_id("ledger", "L_", 6)
                copy["entry_group_id"] = newg
                copy["description"] = le["description"] + " (duplicate)"
                self.ds.ledger_entries.append(copy)
                if le["account"] == "RAZORPAY_CLEARING":
                    clearing = le["amount_paise"]
            self.gt("D3_DUPLICATE_LEDGER", "ledger_entry", newg, s["settlement_id"],
                    "D3_LEDGER", "DUPLICATE_LEDGER_ENTRY", clearing, True,
                    f"Settlement posting group {g} was entered a second time as {newg}. The group is "
                    f"internally balanced, so only the RAZORPAY_CLEARING net exposes it: the clearing "
                    f"account is over-credited by {clearing} paise.",
                    original_field="entry_group_id", original_value=clearing, mutated_value=clearing * 2)
            done += 1

    def d3_missing_ledger(self, count: int) -> None:
        done = 0
        for s in self.pick_settlements(count * 4, claim="d3"):
            if done >= count:
                self.claimed_d3.discard(s["settlement_id"]); continue
            groups = self._settlement_groups(s["settlement_id"])
            if not groups:
                self.claimed_d3.discard(s["settlement_id"]); continue
            g = groups[0]
            legs = [le for le in self.ds.ledger_entries if le["entry_group_id"] == g]
            clearing = next((le["amount_paise"] for le in legs if le["account"] == "RAZORPAY_CLEARING"), 0)
            pid = legs[0]["payment_id"]
            for le in legs:
                self.ds.ledger_entries.remove(le)
            self.gt("D3_MISSING_LEDGER", "ledger_entry", g, s["settlement_id"],
                    "D3_LEDGER", "MISSING_LEDGER_ENTRY", clearing, True,
                    f"The settlement posting for payment {pid} (group {g}) was never made. The "
                    f"clearing account is left {clearing} paise in debit -- money captured but never "
                    f"shown as settled.",
                    original_field="entry_group_id", original_value=clearing, mutated_value=0)
            done += 1

    def d3_wrong_account(self, count: int) -> None:
        done = 0
        for s in self.pick_settlements(count * 6, claim="d3"):
            if done >= count:
                self.claimed_d3.discard(s["settlement_id"]); continue
            leg = next((le for le in self.ds.ledger_entries
                        if le["settlement_id"] == s["settlement_id"]
                        and le["account"] == "GATEWAY_FEES" and le["amount_paise"] > 0), None)
            if leg is None:
                self.claimed_d3.discard(s["settlement_id"]); continue
            amt = leg["amount_paise"]
            leg["account"] = "SALES"
            leg["description"] = leg["description"] + " (misposted to SALES)"
            self.gt("D3_WRONG_ACCOUNT", "ledger_entry", leg["ledger_entry_id"], s["settlement_id"],
                    "D3_LEDGER", "MISPOSTED_ACCOUNT", amt, True,
                    f"{amt} paise of gateway fee was posted to SALES instead of GATEWAY_FEES. The "
                    f"entry group still balances and the clearing account still nets to zero, so "
                    f"only an account-level integrity check finds it -- overstated revenue and "
                    f"understated input GST.",
                    original_field="account", original_value=amt, mutated_value=amt)
            done += 1

    # ================================================================ DELTA 4
    def _settled_allocs(self):
        return [a for a in self.ds.allocations
                if a["allocation_status"] == "SETTLED" and a["allocation_id"] not in self.claimed_alloc]

    def _transfer_for(self, a: dict) -> dict | None:
        return next((t for t in self.ds.transfers
                     if t["payment_id"] == a["payment_id"] and t["seller_id"] == a["seller_id"]
                     and t["transfer_status"] == "PROCESSED"), None)

    def _settlement_of_alloc(self, a: dict) -> str | None:
        p = self.ds.by_payment.get(a["payment_id"])
        return p.get("_settlement_id") if p else None

    def d4_alloc_exceeds_payment(self, count: int) -> None:
        done = 0
        pool = self._settled_allocs(); self.rng.shuffle(pool)
        for a in pool:
            if done >= count:
                break
            p = self.ds.by_payment[a["payment_id"]]
            siblings = [x for x in self.ds.allocations if x["payment_id"] == a["payment_id"]]
            if len(siblings) != 1:
                continue
            seller = self.ds.by_seller[a["seller_id"]]
            excess = bps(p["amount_paise"], 1200)
            if excess <= 0:
                continue
            before = a["gross_allocated_paise"]
            a["gross_allocated_paise"] = before + excess
            a["commission_paise"] = bps(a["gross_allocated_paise"], seller["commission_bps"])
            a["net_seller_paise"] = a["gross_allocated_paise"] - a["commission_paise"]
            t = self._transfer_for(a)
            if t:
                t["amount_paise"] = a["net_seller_paise"]   # keep Delta-4 payout clean
            self.claimed_alloc.add(a["allocation_id"])
            self.gt("D4_ALLOC_EXCEEDS_PAYMENT", "allocation", a["allocation_id"],
                    self._settlement_of_alloc(a), "D4_PAYOUT", "ALLOCATION_EXCEEDS_PAYMENT",
                    excess, True,
                    f"Allocations against payment {a['payment_id']} total "
                    f"{a['gross_allocated_paise']} paise against a payment of {p['amount_paise']} "
                    f"paise -- {excess} paise more than the customer ever paid (INV-B3).",
                    original_field="gross_allocated_paise", original_value=before,
                    mutated_value=a["gross_allocated_paise"])
            done += 1

    def d4_alloc_transfer_divergence(self, count: int) -> None:
        """The transfer is short, but a REVERSED transfer row of exactly the
        missing amount exists -- so the gap is fully explainable."""
        done = 0
        pool = self._settled_allocs(); self.rng.shuffle(pool)
        for a in pool:
            if done >= count:
                break
            t = self._transfer_for(a)
            if not t or a["net_seller_paise"] < 20000:
                continue
            short = bps(a["net_seller_paise"], 2500)
            before = t["amount_paise"]
            t["amount_paise"] = before - short
            rid = self.ds.next_id("transfer", "T_")
            self.ds.transfers.append({
                "transfer_id": rid, "payment_id": a["payment_id"], "seller_id": a["seller_id"],
                "amount_paise": short, "transfer_status": "REVERSED",
                "transfer_date": t["transfer_date"], "transfer_reference": f"RTF{rid}-REV"})
            self.claimed_alloc.add(a["allocation_id"])
            self.gt("D4_ALLOC_TRANSFER_DIVERGENCE", "allocation", a["allocation_id"],
                    self._settlement_of_alloc(a), "D4_PAYOUT", "ALLOCATION_TRANSFER_DIVERGENCE",
                    short, True,
                    f"Seller {a['seller_id']} was allocated {a['net_seller_paise']} paise but only "
                    f"{t['amount_paise']} moved. A REVERSED transfer {rid} for exactly {short} paise "
                    f"accounts for the gap -- explainable, not a mystery.",
                    original_field="transfer.amount_paise", original_value=before,
                    mutated_value=t["amount_paise"])
            done += 1

    def d4_transfer_missing(self, count: int) -> None:
        done = 0
        pool = self._settled_allocs(); self.rng.shuffle(pool)
        for a in pool:
            if done >= count:
                break
            t = self._transfer_for(a)
            if not t:
                continue
            self.ds.transfers.remove(t)
            for it in [i for i in self.ds.settlement_items
                       if i["transfer_id"] == t["transfer_id"]]:
                self.ds.settlement_items.remove(it)   # no dangling references
            self.claimed_alloc.add(a["allocation_id"])
            self.gt("D4_TRANSFER_MISSING", "allocation", a["allocation_id"],
                    self._settlement_of_alloc(a), "D4_PAYOUT", "TRANSFER_MISSING",
                    a["net_seller_paise"], True,
                    f"Allocation {a['allocation_id']} is SETTLED and owes seller {a['seller_id']} "
                    f"{a['net_seller_paise']} paise, but no transfer row exists at all. The "
                    f"platform's own settlement can still reconcile perfectly while this is true.",
                    original_field="transfer.amount_paise", original_value=a["net_seller_paise"],
                    mutated_value=0)
            done += 1

    # ========================================================== UNRESOLVABLE
    def unresolvable_phantom_debit(self, count: int) -> None:
        for s in self.pick_settlements(count):
            amount = self.rng.randrange(20000, 500000, 100)
            before = s["net_settlement_amount_paise"]
            s["net_settlement_amount_paise"] = before - amount
            self.sync_bank(s)
            self.gt("UNRESOLVABLE_PHANTOM_DEBIT", "settlement", s["settlement_id"], s["settlement_id"],
                    "D1_COMPUTE", "UNEXPLAINED_SHORTFALL", amount, False,
                    f"The payout is {amount} paise short of policy and NOTHING in any source table "
                    f"accounts for it: no refund, no adjustment, no fee drift, no rollup error. The "
                    f"only correct answer is to report the exact rupee figure as unexplained.",
                    original_field="net_settlement_amount_paise", original_value=before,
                    mutated_value=before - amount)

    def unresolvable_ambiguous_credit(self, count: int) -> None:
        for s in self.pick_settlements(count, pred=lambda s: s["net_settlement_amount_paise"] > 50000):
            b = self._bank_for(s["settlement_id"])
            if not b:
                continue
            net = s["net_settlement_amount_paise"]
            b["settlement_utr"] = None
            b["description"] = "NEFT-RAZORPAYSOFTWAREPVTLTD-SETTLEMENT"
            b["credit_paise"] = net
            b["_settlement_id"] = None
            bid = self.ds.next_id("bank", "B_")
            self.ds.bank_transactions.append({
                "bank_transaction_id": bid, "transaction_date": b["transaction_date"],
                "description": "NEFT-RAZORPAYSOFTWAREPVTLTD-SETTLEMENT",
                "credit_paise": net, "debit_paise": 0, "bank_reference": f"BREF{bid}",
                "settlement_utr": None, "_settlement_id": None})
            self.gt("UNRESOLVABLE_AMBIGUOUS_CREDIT", "settlement", s["settlement_id"],
                    s["settlement_id"], "D2_BANK", "AMBIGUOUS_BANK_MATCH", net, False,
                    f"Two bank credits of exactly {net} paise on the same date, no UTR on either, "
                    f"and no other field distinguishes them. They are genuinely indistinguishable. "
                    f"Picking one would be a guess dressed up as a match.")

    def unresolvable_phantom_payout_gap(self, count: int) -> None:
        done = 0
        pool = self._settled_allocs(); self.rng.shuffle(pool)
        for a in pool:
            if done >= count:
                break
            t = self._transfer_for(a)
            if not t or a["net_seller_paise"] < 20000:
                continue
            short = bps(a["net_seller_paise"], 1800)
            before = t["amount_paise"]
            t["amount_paise"] = before - short
            self.claimed_alloc.add(a["allocation_id"])
            self.gt("UNRESOLVABLE_PHANTOM_PAYOUT_GAP", "allocation", a["allocation_id"],
                    self._settlement_of_alloc(a), "D4_PAYOUT", "PHANTOM_PAYOUT_GAP",
                    short, False,
                    f"Seller {a['seller_id']} is {short} paise short on allocation "
                    f"{a['allocation_id']}, and unlike the divergence cases there is NO reversed "
                    f"transfer, no adjustment and no second transfer explaining it. Report the "
                    f"rupees, name the seller, escalate.",
                    original_field="transfer.amount_paise", original_value=before,
                    mutated_value=t["amount_paise"])
            done += 1


PRE_LEDGER_PLAN = [
    ("d1_fee_rate_drift", 4), ("d1_tax_aggregate_rounding", 3), ("d1_refund_not_deducted", 2),
    ("d1_refund_partial_multi", 1), ("d1_refund_outside_period", 1),
    ("d1_header_rollup_mismatch", 2), ("d1_adjustment_applied", 3),
    ("d1_fee_corrected_by_adjustment", 1),
    ("d2_timing_next_day", 5), ("d2_narration_no_utr", 5), ("d2_merged_credit", 3),
    ("d2_split_credit", 2), ("d2_settlement_on_hold", 2), ("d2_suffix_collision", 1),
    ("d2_same_amount_same_day", 1),
    ("d4_alloc_exceeds_payment", 2), ("d4_alloc_transfer_divergence", 3), ("d4_transfer_missing", 2),
    ("unresolvable_phantom_debit", 3), ("unresolvable_ambiguous_credit", 2),
    ("unresolvable_phantom_payout_gap", 2),
]


LEDGER_PLAN = [
    ("d3_duplicate_ledger", 3), ("d3_missing_ledger", 2), ("d3_wrong_account", 2),
]


def _apply(ds, rng: random.Random, plan, planter=None) -> "Planter":
    p = planter or Planter(ds, rng)
    for name, count in plan:
        getattr(p, name)(count)
        ds.index()
    ds.index()
    return p


def apply_anomalies_pre_ledger(ds, rng: random.Random) -> None:
    """Everything that mutates payments, refunds, settlements, transfers or the
    bank statement. Runs BEFORE the ledger is posted so the books are written
    against the final state of the money."""
    ds._planter = _apply(ds, rng, PRE_LEDGER_PLAN)


def apply_anomalies_ledger(ds, rng: random.Random) -> None:
    """Ledger-only mutations. These run AFTER posting, because duplicating or
    deleting a posting only means anything once the correct postings exist."""
    p = getattr(ds, "_planter", None) or Planter(ds, rng)
    p.rng = rng
    _apply(ds, rng, LEDGER_PLAN, planter=p)
