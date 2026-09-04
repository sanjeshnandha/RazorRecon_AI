"""
Loads the static evaluation batch into Postgres.

The batch has a CONSTANT dataset_id, so loading it twice replaces it rather than
piling up copies, and an evaluator can always find it. Everything it writes comes
from evaluation_batch.json -- this module contains no amounts of its own, only
the mechanics of turning that file into rows the real engine can reconcile.

    python -m fixtures.loader            # load, then reconcile
    python -m fixtures.loader --no-run   # load only
"""
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import date, datetime, time, timedelta, timezone

from engine.db import connect, copy_rows
from engine.money import bps
from engine.policy import load_policy

BATCH = pathlib.Path(__file__).resolve().parent / "evaluation_batch.json"
IST = timezone(timedelta(hours=5, minutes=30))


def _d(x) -> date:
    return date.fromisoformat(x) if isinstance(x, str) else x


def _ts(x) -> datetime:
    return datetime.combine(_d(x), time(11, 0), IST)


def load_batch() -> dict:
    return json.loads(BATCH.read_text())


def _clean_header(sc: dict) -> dict:
    """What the settlement header would say if nothing were wrong with it."""
    cap = [p for p in sc["payments"] if p["payment_status"] == "CAPTURED"]
    gross = sum(p["amount_paise"] for p in cap)
    f = sum(p["charged_fee_paise"] for p in cap)
    t = sc.get("aggregate_tax_paise") or sum(p["charged_tax_paise"] for p in cap)
    ref = sum(r["refund_amount_paise"] for r in sc["refunds"]
              if r["refund_status"] == "PROCESSED" and r["itemised"]
              and not r.get("itemise_in"))
    adj = sum(a["amount_paise"] for a in sc["adjustments"] if a["itemised"])
    return {"gross": gross, "fee": f, "tax": t, "refund": ref, "adjustment": adj,
            "net": gross - ref - f - t + adj}


def load(conn, batch: dict | None = None) -> dict:
    batch = batch or load_batch()
    ds = batch["dataset_id"]
    policy = load_policy()
    scenarios = batch["scenarios"]

    with conn.cursor() as cur:
        cur.execute("DELETE FROM datasets WHERE dataset_id=%s", (ds,))
    conn.commit()

    # The population comes from the batch file, like everything else. It used to
    # be one hardcoded customer here -- fine for proving arithmetic, but it made
    # 35 payments look like they came from the same person.
    customers = [(ds, c["customer_id"], c["name"], c["email"], _ts(c["created_at"]))
                 for c in batch.get("customers", [])]
    sellers, orders, payments, refunds, allocs, xfers, adjs = [], [], [], [], [], [], []
    settlements, items, bank, ledger, edges, truth = [], [], [], [], [], []
    seen_sellers: set[str] = set()
    seen_orders: set[str] = set()
    n_item = n_bank = n_led = n_grp = n_gt = 0

    # ---- pass 1: the records, the headers, and the standard ledger ----------
    headers: dict[str, dict] = {}
    foreign_refunds: dict[str, list] = {}
    for sc in scenarios:
        s = sc["settlement"]
        sid = s["settlement_id"]
        head = _clean_header(sc)
        ov = sc.get("header_override") or {}
        head["gross"] += ov.get("gross_delta_paise", 0)
        head["net"] += ov.get("net_delta_paise", 0)
        headers[sid] = head

        for x in sc["sellers"]:
            if x["seller_id"] in seen_sellers:
                continue
            seen_sellers.add(x["seller_id"])
            sellers.append((ds, x["seller_id"], x["seller_name"], x["seller_type"],
                            x["commission_bps"], x.get("status", "ACTIVE")))

        for p in sc["payments"]:
            if p["order_id"] not in seen_orders:
                seen_orders.add(p["order_id"])
                orders.append((ds, p["order_id"], p["customer_id"], p["amount_paise"],
                               _d(p["captured_at"]), "PAID"))
            cap = p["payment_status"] == "CAPTURED"
            payments.append((ds, p["payment_id"], p["order_id"], p["customer_id"],
                             p["amount_paise"],
                             p["payment_status"], p["payment_method"], _ts(p["captured_at"]),
                             _ts(p["captured_at"]) if cap else None,
                             None if cap else "Bank declined"))
        for r in sc["refunds"]:
            refunds.append((ds, r["refund_id"], r["payment_id"], r["refund_amount_paise"],
                            r["refund_status"], _d(r["refund_date"]), "evaluation batch"))
        for a in sc["adjustments"]:
            adjs.append((ds, a["adjustment_id"], sid, a["adjustment_type"], a["amount_paise"],
                         f"{a['adjustment_type']} on {sid}", _ts(s["settlement_date"]),
                         a["status"], a.get("ref_payment_id")))
        for a in sc["allocations"]:
            allocs.append((ds, a["allocation_id"], a["payment_id"], a["seller_id"],
                           a["gross_allocated_paise"], a["commission_paise"],
                           a["net_seller_paise"], a["allocation_status"],
                           _d(s["settlement_period_end"])))
        for t in sc["transfers"]:
            xfers.append((ds, t["transfer_id"], t["payment_id"], t["seller_id"],
                          t["seller_amount_paise"], t["transfer_status"],
                          _d(s["settlement_date"]), t["transfer_id"]))

        for r in foreign_refunds.get(sid, []):
            head["refund"] += r["refund_amount_paise"]
            head["net"] -= r["refund_amount_paise"]
        settlements.append((ds, sid, _d(s["settlement_date"]), _d(s["settlement_period_start"]),
                            _d(s["settlement_period_end"]), head["gross"], head["refund"],
                            head["fee"], head["tax"], head["adjustment"], head["net"],
                            s["settlement_status"], s["settlement_utr"]))

        # ---- settlement items ----
        cap_items = []
        for p in sc["payments"]:
            if p["payment_status"] != "CAPTURED":
                continue          # INV-B5: a failed payment never reaches a settlement
            n_item += 1
            cap_items.append(len(items))
            items.append([ds, f"SI_{n_item:04d}", sid, "PAYMENT", p["payment_id"], None, None,
                          None, p["amount_paise"], p["charged_fee_paise"],
                          p["charged_tax_paise"], _d(p["captured_at"])])
        # the aggregate-GST scenario: the header taxed the total, so the paise of
        # difference has to live on a line for the items to roll up to the header
        if sc.get("aggregate_tax_paise") and cap_items:
            per_item = sum(items[i][10] for i in cap_items)
            biggest = max(cap_items, key=lambda i: items[i][8])
            items[biggest][10] += sc["aggregate_tax_paise"] - per_item
        for r in sc["refunds"]:
            if not r["itemised"]:
                continue
            n_item += 1
            # a refund dated past this period's close belongs to the settlement
            # whose period DOES contain it, and is itemised there instead
            target = r.get("itemise_in") or sid
            items.append([ds, f"SI_{n_item:04d}", target, "REFUND", None, r["refund_id"], None,
                          None, -r["refund_amount_paise"], 0, 0, _d(r["refund_date"])])
            if target != sid:
                foreign_refunds.setdefault(target, []).append(r)
        for a in sc["adjustments"]:
            if not a["itemised"]:
                continue
            n_item += 1
            items.append([ds, f"SI_{n_item:04d}", sid, "ADJUSTMENT", None, None,
                          a["adjustment_id"], None, a["amount_paise"], 0, 0,
                          _d(s["settlement_date"])])

        # ---- ledger: the standard three postings, then any mutation ----
        mut = sc.get("ledger_mutation") or {}
        for p in sc["payments"]:
            if p["payment_status"] != "CAPTURED":
                continue
            g = p["amount_paise"]
            r_total = sum(r["refund_amount_paise"] for r in sc["refunds"]
                          if r["payment_id"] == p["payment_id"]
                          and r["refund_status"] == "PROCESSED")
            f, t = p["charged_fee_paise"], p["charged_tax_paise"]
            c = g - r_total
            net = c - f - t

            n_grp += 1
            gcap = f"G_{n_grp:04d}"
            for acct, dirn in (("RAZORPAY_CLEARING", "DR"), ("SALES", "CR")):
                n_led += 1
                ledger.append([ds, f"L_{n_led:05d}", gcap, acct, dirn, g, p["order_id"],
                               p["payment_id"], None, None, None, _d(p["captured_at"]),
                               "capture"])
            for r in sc["refunds"]:
                if r["payment_id"] != p["payment_id"] or r["refund_status"] != "PROCESSED":
                    continue
                n_grp += 1
                gref = f"G_{n_grp:04d}"
                for acct, dirn in (("REFUNDS", "DR"), ("RAZORPAY_CLEARING", "CR")):
                    n_led += 1
                    ledger.append([ds, f"L_{n_led:05d}", gref, acct, dirn,
                                   r["refund_amount_paise"], None, p["payment_id"],
                                   r["refund_id"], None, None, _d(r["refund_date"]), "refund"])

            if mut.get("kind") == "DROP_SETTLEMENT_GROUP" and mut["payment_id"] == p["payment_id"]:
                continue          # the posting that never happened
            copies = 2 if (mut.get("kind") == "DUPLICATE_SETTLEMENT_GROUP"
                           and mut["payment_id"] == p["payment_id"]) else 1
            for copy_i in range(copies):
                n_grp += 1
                gset = f"G_{n_grp:04d}"
                legs = []
                if net > 0:
                    legs.append(("BANK", "DR", net))
                elif net < 0:
                    legs.append(("BANK", "CR", -net))
                fee_account = "GATEWAY_FEES"
                if (mut.get("kind") == "MISPOST_FEE_TO_SALES"
                        and mut["payment_id"] == p["payment_id"] and copy_i == 0):
                    fee_account = "SALES"
                if f:
                    legs.append((fee_account, "DR", f))
                if t:
                    legs.append(("INPUT_GST", "DR", t))
                if c:
                    legs.append(("RAZORPAY_CLEARING", "CR", c))
                for acct, dirn, amt in legs:
                    n_led += 1
                    ledger.append([ds, f"L_{n_led:05d}", gset, acct, dirn, amt, None,
                                   p["payment_id"], None, sid, None,
                                   _d(s["settlement_date"]),
                                   "settlement" if copy_i == 0 else "settlement (duplicate)"])

        for g in sc["ground_truth"]:
            n_gt += 1
            truth.append((ds, f"GT_{n_gt:04d}", g["anomaly_type"], g["subject_type"],
                          g["subject_id"], sid, g["expected_delta_kind"],
                          g["expected_exception_type"], None, None, None,
                          g["planted_amount_paise"], g["is_resolvable"], g["notes"]))

    # ---- the pipeline: captured, not yet settled ---------------------------
    # Additive by construction. These payments belong to no settlement, so they
    # appear in no delta and disturb no scenario -- they are the forecastable
    # inflow, and their PENDING allocations are the forecastable outflow.
    for day in batch.get("pipeline", []):
        for cap in day["captures"]:
            p = cap["payment"]
            if p["order_id"] not in seen_orders:
                seen_orders.add(p["order_id"])
                orders.append((ds, p["order_id"], p["customer_id"], p["amount_paise"],
                               _d(p["captured_at"]), "PAID"))
            payments.append((ds, p["payment_id"], p["order_id"], p["customer_id"],
                             p["amount_paise"], "CAPTURED", p["payment_method"],
                             _ts(p["captured_at"]), _ts(p["captured_at"]), None))
            for a in cap["allocations"]:
                allocs.append((ds, a["allocation_id"], a["payment_id"], a["seller_id"],
                               a["gross_allocated_paise"], a["commission_paise"],
                               a["net_seller_paise"], a["allocation_status"],
                               _d(p["captured_at"])))
            # Capture posting only. There is no settlement posting because there
            # has been no settlement, so RAZORPAY_CLEARING stays open for these
            # payments -- which is what "money taken but not yet paid out" means.
            n_grp += 1
            gcap = f"G_{n_grp:04d}"
            for acct, dirn in (("RAZORPAY_CLEARING", "DR"), ("SALES", "CR")):
                n_led += 1
                ledger.append([ds, f"L_{n_led:05d}", gcap, acct, dirn, p["amount_paise"],
                               p["order_id"], p["payment_id"], None, None, None,
                               _d(p["captured_at"]), "capture (awaiting settlement)"])

    # Every seller in the population gets a row, including any that took no money
    # in this batch -- a marketplace roster is not only its active sellers.
    for x in batch.get("sellers", []):
        if x["seller_id"] not in seen_sellers:
            seen_sellers.add(x["seller_id"])
            sellers.append((ds, x["seller_id"], x["seller_name"], x["seller_type"],
                            x["commission_bps"], x.get("status", "ACTIVE")))

    # ---- pass 2: the bank statement, which needs every net to be known -------
    by_id = {sc["settlement"]["settlement_id"]: sc for sc in scenarios}
    merge_head = next((s for s in scenarios if s["bank"] == "MERGE_HEAD"), None)
    merge_tail = next((s for s in scenarios if s["bank"] == "MERGE_TAIL"), None)
    amb_head = next((s for s in scenarios if s["bank"] == "AMBIGUOUS_HEAD"), None)
    amb_tail = next((s for s in scenarios if s["bank"] == "AMBIGUOUS_TAIL"), None)

    def add_bank(sid, amount, day, description, utr):
        nonlocal n_bank
        n_bank += 1
        bank.append((ds, f"B_{n_bank:04d}", _d(day), description, amount, 0,
                     f"BREF{n_bank:06d}", utr))

    for sc in scenarios:
        s = sc["settlement"]
        sid, mode = s["settlement_id"], sc["bank"]
        net, day, u = headers[sid]["net"], s["settlement_date"], s["settlement_utr"]
        if mode == []:                                   # money never arrived
            continue
        if mode == "MERGE_TAIL":                         # paid inside its partner's credit
            continue
        if mode is None:
            add_bank(sid, net, day, f"NEFT CR-RAZORPAY SOFTWARE-{u}", u)
        elif mode == "SPLIT":
            first = net // 3
            add_bank(sid, first, day, "NEFT-RAZORPAYSOFTWAREPVTLTD-SETTLEMENT PART 1", None)
            add_bank(sid, net - first, day, "NEFT-RAZORPAYSOFTWAREPVTLTD-SETTLEMENT PART 2", None)
        elif mode == "MERGE_HEAD":
            partner = headers[merge_tail["settlement"]["settlement_id"]]["net"]
            latest = max(_d(day), _d(merge_tail["settlement"]["settlement_date"]))
            add_bank(sid, net + partner, latest,
                     "NEFT-RAZORPAYSOFTWAREPVTLTD-BULK SETTLEMENT", None)
        elif mode == "SUFFIX":
            add_bank(sid, net, day, f"NEFT CR RZRPAY {u[-8:]}", None)
        elif mode in ("AMBIGUOUS_HEAD", "AMBIGUOUS_TAIL"):
            # both credits are written on the head's date so nothing separates them
            when = amb_head["settlement"]["settlement_date"]
            add_bank(sid, net, when, "NEFT-RAZORPAYSOFTWAREPVTLTD-SETTLEMENT", None)

    # ---- money edges, so Trace Money works on the batch ----------------------
    seen_edge = set()

    def edge(st, si, dt, di, kind, amt):
        if si is None or di is None or (st, si, dt, di, kind) in seen_edge:
            return
        seen_edge.add((st, si, dt, di, kind))
        edges.append((ds, st, si, dt, di, kind, amt))

    for o in orders:
        edge("customer", o[2], "order", o[1], "PLACED", o[3])
    for p in payments:
        edge("order", p[2], "payment", p[1], "PAID_BY", p[4])
    for r in refunds:
        edge("payment", r[2], "refund", r[1], "REFUNDED_BY", r[3])
    for a in allocs:
        edge("payment", a[2], "seller_allocation", a[1], "ALLOCATED_TO", a[4])
    for t in xfers:
        edge("payment", t[2], "transfer", t[1], "TRANSFERRED_BY", t[4])
    for it in items:
        src = (("payment", it[4]) if it[4] else ("refund", it[5]) if it[5]
               else ("adjustment", it[6]) if it[6] else ("transfer", it[7]))
        edge(src[0], src[1], "settlement_item", it[1], "SETTLED_AS", it[8])
        edge("settlement_item", it[1], "settlement", it[2], "PART_OF", it[8])
    for le in ledger:
        edge("payment", le[7], "ledger_entry", le[1], "POSTED_AS", le[5])
        edge("settlement", le[9], "ledger_entry", le[1], "POSTED_AS", le[5])

    counts = {"customers": len(customers), "sellers": len(sellers), "orders": len(orders),
              "payments": len(payments), "refunds": len(refunds),
              "seller_allocations": len(allocs), "transfers": len(xfers),
              "adjustments": len(adjs), "settlements": len(settlements),
              "settlement_items": len(items), "bank_transactions": len(bank),
              "ledger_entries": len(ledger), "money_edges": len(edges),
              "ground_truth_anomalies": len(truth)}
    counts["total_financial_records"] = sum(
        counts[k] for k in ("payments", "refunds", "seller_allocations", "transfers",
                            "adjustments", "settlement_items", "bank_transactions",
                            "ledger_entries"))
    counts["scenarios"] = len(scenarios)
    counts["static_batch"] = True

    with conn.cursor() as cur:
        cur.execute("INSERT INTO datasets (dataset_id, seed, policy_version, row_counts, label) "
                    "VALUES (%s,0,%s,%s,%s)",
                    (ds, policy.version, json.dumps(counts), "evaluation-batch"))

    copy_rows(conn, "customers", ["dataset_id","customer_id","name","email","created_at"], customers)
    if sellers:
        copy_rows(conn, "sellers", ["dataset_id","seller_id","seller_name","seller_type",
                                    "commission_bps","status"], sellers)
    copy_rows(conn, "orders", ["dataset_id","order_id","customer_id","order_amount_paise",
                               "order_date","order_status"], orders)
    copy_rows(conn, "payments", ["dataset_id","payment_id","order_id","customer_id","amount_paise",
                                 "payment_status","payment_method","created_at","captured_at",
                                 "failure_reason"], payments)
    if refunds:
        copy_rows(conn, "refunds", ["dataset_id","refund_id","payment_id","refund_amount_paise",
                                    "refund_status","refund_date","refund_reason"], refunds)
    if allocs:
        copy_rows(conn, "seller_allocations",
                  ["dataset_id","allocation_id","payment_id","seller_id","gross_allocated_paise",
                   "commission_paise","net_seller_paise","allocation_status","allocation_date"],
                  allocs)
    if xfers:
        copy_rows(conn, "transfers", ["dataset_id","transfer_id","payment_id","seller_id",
                                      "amount_paise","transfer_status","transfer_date",
                                      "transfer_reference"], xfers)
    if adjs:
        copy_rows(conn, "adjustments", ["dataset_id","adjustment_id","settlement_id",
                                        "adjustment_type","amount_paise","reason","created_at",
                                        "status","ref_payment_id"], adjs)
    copy_rows(conn, "settlements",
              ["dataset_id","settlement_id","settlement_date","settlement_period_start",
               "settlement_period_end","gross_amount_paise","refund_amount_paise",
               "fee_amount_paise","tax_amount_paise","adjustment_amount_paise",
               "net_settlement_amount_paise","settlement_status","settlement_utr"], settlements)
    copy_rows(conn, "settlement_items",
              ["dataset_id","settlement_item_id","settlement_id","transaction_type","payment_id",
               "refund_id","adjustment_id","transfer_id","amount_paise","fee_paise","tax_paise",
               "transaction_date"], [tuple(i) for i in items])
    if bank:
        copy_rows(conn, "bank_transactions",
                  ["dataset_id","bank_transaction_id","transaction_date","description",
                   "credit_paise","debit_paise","bank_reference","settlement_utr"], bank)
    copy_rows(conn, "ledger_entries",
              ["dataset_id","ledger_entry_id","entry_group_id","account","direction","amount_paise",
               "order_id","payment_id","refund_id","settlement_id","seller_id","ledger_date",
               "description"], [tuple(l) for l in ledger])
    copy_rows(conn, "money_edges", ["dataset_id","src_type","src_id","dst_type","dst_id",
                                    "edge_kind","amount_paise"], edges)
    copy_rows(conn, "ground_truth_anomalies",
              ["dataset_id","anomaly_id","anomaly_type","subject_type","subject_id","settlement_id",
               "expected_delta_kind","expected_exception_type","original_field",
               "original_value_paise","mutated_value_paise","planted_amount_paise",
               "is_resolvable","notes"], truth)
    conn.commit()
    return {"dataset_id": ds, "row_counts": counts}


def main() -> None:
    ap = argparse.ArgumentParser(description="Load the static evaluation batch.")
    ap.add_argument("--no-run", action="store_true", help="load without reconciling")
    args = ap.parse_args()
    with connect() as conn:
        out = load(conn)
        print(f"dataset_id = {out['dataset_id']}   label = evaluation-batch")
        for k, v in out["row_counts"].items():
            if isinstance(v, int):
                print(f"  {k:26s} {v:>7,d}")
        if not args.no_run:
            from engine import runner
            m = runner.run(conn, out["dataset_id"])
            print(f"  run                        {m['run_id']}")


if __name__ == "__main__":
    main()
