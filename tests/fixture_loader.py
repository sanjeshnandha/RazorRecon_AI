"""
Loads a hand-worked Phase 0 fixture into Postgres as a one-settlement dataset,
so the real engine -- not a stub -- runs against numbers a human computed from
policy.yaml with no code involved.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

from engine.db import copy_rows

IST = timezone(timedelta(hours=5, minutes=30))


def _d(x):
    return date.fromisoformat(x) if isinstance(x, str) else x


def load_fixture(conn, fx: dict) -> str:
    ds_id = str(uuid.uuid4())
    s = fx["settlement"]
    sid = s["settlement_id"]
    pay = fx.get("payments", [])
    refs = fx.get("refunds", [])
    adjs = fx.get("adjustments", [])
    allocs = fx.get("allocations", [])
    xfers = fx.get("transfers", [])
    sellers = fx.get("sellers", [])
    groups = fx.get("ledger_entry_groups", [])
    exp = fx["expected"]

    with conn.cursor() as cur:
        cur.execute("INSERT INTO datasets (dataset_id, seed, policy_version, row_counts, label) "
                    "VALUES (%s,0,'1.0.0',%s,%s)",
                    (ds_id, json.dumps({"fixture": fx["fixture_id"], "total_financial_records":
                                        len(pay) + len(refs) + len(adjs) + len(allocs) + len(xfers)}),
                     fx["fixture_id"]))
    cust = ("C_FX",)
    copy_rows(conn, "customers", ["dataset_id","customer_id","name","email","created_at"],
              [(ds_id, "C_FX", "Fixture Customer", "fx@example.in", datetime(2025, 12, 1, tzinfo=IST))])
    if sellers:
        copy_rows(conn, "sellers",
                  ["dataset_id","seller_id","seller_name","seller_type","commission_bps","status"],
                  [(ds_id, x["seller_id"], x["seller_id"], x["seller_type"], x["commission_bps"], "ACTIVE")
                   for x in sellers])
    copy_rows(conn, "orders",
              ["dataset_id","order_id","customer_id","order_amount_paise","order_date","order_status"],
              [(ds_id, f"O_{p['payment_id']}", "C_FX", p["amount_paise"],
                _d(p.get("captured_at", str(s["settlement_period_start"]))), "PAID") for p in pay])
    copy_rows(conn, "payments",
              ["dataset_id","payment_id","order_id","customer_id","amount_paise","payment_status",
               "payment_method","created_at","captured_at","failure_reason"],
              [(ds_id, p["payment_id"], f"O_{p['payment_id']}", "C_FX", p["amount_paise"], "CAPTURED",
                p["payment_method"], datetime.combine(_d(p["captured_at"]), datetime.min.time(), IST),
                datetime.combine(_d(p["captured_at"]), datetime.min.time(), IST), None) for p in pay])
    if refs:
        copy_rows(conn, "refunds",
                  ["dataset_id","refund_id","payment_id","refund_amount_paise","refund_status",
                   "refund_date","refund_reason"],
                  [(ds_id, r["refund_id"], r["payment_id"], r["refund_amount_paise"],
                    r["refund_status"], _d(r["refund_date"]), "fixture") for r in refs])
    if adjs:
        copy_rows(conn, "adjustments",
                  ["dataset_id","adjustment_id","settlement_id","adjustment_type","amount_paise",
                   "reason","created_at","status","ref_payment_id"],
                  [(ds_id, a["adjustment_id"], a.get("settlement_id", sid), a["adjustment_type"],
                    a["amount_paise"], "fixture", datetime.combine(_d(s["settlement_date"]),
                    datetime.min.time(), IST), a["status"], a.get("ref_payment_id")) for a in adjs])
    if allocs:
        copy_rows(conn, "seller_allocations",
                  ["dataset_id","allocation_id","payment_id","seller_id","gross_allocated_paise",
                   "commission_paise","net_seller_paise","allocation_status","allocation_date"],
                  [(ds_id, a["allocation_id"], a["payment_id"], a["seller_id"],
                    a["gross_allocated_paise"], a["commission_paise"], a["net_seller_paise"],
                    a["allocation_status"], _d(s["settlement_period_end"])) for a in allocs])
    if xfers:
        copy_rows(conn, "transfers",
                  ["dataset_id","transfer_id","payment_id","seller_id","amount_paise",
                   "transfer_status","transfer_date","transfer_reference"],
                  [(ds_id, t["transfer_id"], t["payment_id"], t["seller_id"], t["amount_paise"],
                    t["transfer_status"], _d(s["settlement_date"]), t["transfer_id"]) for t in xfers])

    # ---- settlement header. The fixture's own actual_net is authoritative;
    #      the components are whatever the fixture says they are.
    gross = s.get("header_gross_amount_paise",
                  exp.get("gross_paise", sum(p["amount_paise"] for p in pay)))
    header_refund = sum(r["refund_amount_paise"] for r in refs
                        if r.get("present_in_settlement_items", r.get("in_period", False)))
    charged_fee = sum(p.get("charged_fee_paise", p.get("policy_fee_paise", 0)) for p in pay)
    charged_tax = sum(p.get("charged_tax_paise", p.get("policy_tax_paise", 0)) for p in pay)
    if "aggregate_tax_paise" in exp:
        charged_tax = exp["aggregate_tax_paise"]
    header_adj = sum(a["amount_paise"] for a in adjs if a.get("present_in_settlement_items"))
    # When the fixture states the actual net, that figure is authoritative --
    # it is the number a human worked out. Otherwise the settlement is clean by
    # construction and the header follows from its own components.
    actual_net = exp.get("actual_net_paise",
                         gross - header_refund - charged_fee - charged_tax + header_adj)

    copy_rows(conn, "settlements",
              ["dataset_id","settlement_id","settlement_date","settlement_period_start",
               "settlement_period_end","gross_amount_paise","refund_amount_paise","fee_amount_paise",
               "tax_amount_paise","adjustment_amount_paise","net_settlement_amount_paise",
               "settlement_status","settlement_utr"],
              [(ds_id, sid, _d(s["settlement_date"]), _d(s["settlement_period_start"]),
                _d(s["settlement_period_end"]), gross, header_refund, charged_fee, charged_tax,
                header_adj, actual_net, s["settlement_status"], s.get("settlement_utr"))])

    items = []
    n = 0
    for p in pay:
        n += 1
        # the per-item tax carries the aggregate remainder when the fixture is
        # exercising the aggregate-GST method error
        tx = p.get("charged_tax_paise", p.get("policy_tax_paise", 0))
        items.append((ds_id, f"SI_{n:04d}", sid, "PAYMENT", p["payment_id"], None, None, None,
                      p["amount_paise"], p.get("charged_fee_paise", p.get("policy_fee_paise", 0)),
                      tx, _d(p["captured_at"])))
    if "aggregate_tax_paise" in exp and items:
        diff = exp["aggregate_tax_paise"] - exp["computed_tax_paise"]
        big = max(range(len(items)), key=lambda i: items[i][9])
        row = list(items[big]); row[10] += diff; items[big] = tuple(row)
    for r in refs:
        if not r.get("present_in_settlement_items", r.get("in_period", False)):
            continue
        n += 1
        items.append((ds_id, f"SI_{n:04d}", sid, "REFUND", None, r["refund_id"], None, None,
                      -r["refund_amount_paise"], 0, 0, _d(r["refund_date"])))
    for a in adjs:
        if not a.get("present_in_settlement_items"):
            continue
        n += 1
        items.append((ds_id, f"SI_{n:04d}", sid, "ADJUSTMENT", None, None, a["adjustment_id"], None,
                      a["amount_paise"], 0, 0, _d(s["settlement_date"])))
    copy_rows(conn, "settlement_items",
              ["dataset_id","settlement_item_id","settlement_id","transaction_type","payment_id",
               "refund_id","adjustment_id","transfer_id","amount_paise","fee_paise","tax_paise",
               "transaction_date"], items)

    if s.get("settlement_utr") and actual_net > 0:
        copy_rows(conn, "bank_transactions",
                  ["dataset_id","bank_transaction_id","transaction_date","description","credit_paise",
                   "debit_paise","bank_reference","settlement_utr"],
                  [(ds_id, "B_FX01", _d(s["settlement_date"]),
                    f"NEFT CR-RAZORPAY SOFTWARE-{s['settlement_utr']}", actual_net, 0,
                    "BREFFX01", s["settlement_utr"])])

    # ---- ledger. Explicit groups when the fixture states them, otherwise the
    #      standard three postings so Delta-3 is clean by construction.
    led = []
    m = 0
    if groups:
        for g in groups:
            for leg in g["legs"]:
                m += 1
                led.append((ds_id, f"L_{m:04d}", g["entry_group_id"], leg["account"],
                            leg["direction"], leg["amount_paise"], None, pay[0]["payment_id"], None,
                            sid if g["event"].startswith("settlement") else None, None,
                            _d(s["settlement_date"]), g["event"]))
    else:
        for p in pay:
            g = p["amount_paise"]
            r_total = sum(r["refund_amount_paise"] for r in refs
                          if r["payment_id"] == p["payment_id"] and r["refund_status"] == "PROCESSED")
            f = p.get("charged_fee_paise", p.get("policy_fee_paise", 0))
            t = p.get("charged_tax_paise", p.get("policy_tax_paise", 0))
            c = g - r_total
            nn = c - f - t
            m += 1; gid = f"G_{m:04d}"
            led.append((ds_id, f"L_C{m:04d}", gid, "RAZORPAY_CLEARING", "DR", g, None,
                        p["payment_id"], None, None, None, _d(p["captured_at"]), "capture"))
            led.append((ds_id, f"L_S{m:04d}", gid, "SALES", "CR", g, None, p["payment_id"], None,
                        None, None, _d(p["captured_at"]), "capture"))
            for r in refs:
                if r["payment_id"] != p["payment_id"] or r["refund_status"] != "PROCESSED":
                    continue
                m += 1; rg = f"G_{m:04d}"
                led.append((ds_id, f"L_RD{m:04d}", rg, "REFUNDS", "DR", r["refund_amount_paise"],
                            None, p["payment_id"], r["refund_id"], None, None, _d(r["refund_date"]), "refund"))
                led.append((ds_id, f"L_RC{m:04d}", rg, "RAZORPAY_CLEARING", "CR",
                            r["refund_amount_paise"], None, p["payment_id"], r["refund_id"], None,
                            None, _d(r["refund_date"]), "refund"))
            m += 1; sg = f"G_{m:04d}"
            if nn > 0:
                led.append((ds_id, f"L_B{m:04d}", sg, "BANK", "DR", nn, None, p["payment_id"], None,
                            sid, None, _d(s["settlement_date"]), "settlement"))
            elif nn < 0:
                led.append((ds_id, f"L_B{m:04d}", sg, "BANK", "CR", -nn, None, p["payment_id"], None,
                            sid, None, _d(s["settlement_date"]), "settlement"))
            if f:
                led.append((ds_id, f"L_F{m:04d}", sg, "GATEWAY_FEES", "DR", f, None, p["payment_id"],
                            None, sid, None, _d(s["settlement_date"]), "settlement fee"))
            if t:
                led.append((ds_id, f"L_G{m:04d}", sg, "INPUT_GST", "DR", t, None, p["payment_id"],
                            None, sid, None, _d(s["settlement_date"]), "settlement gst"))
            if c:
                led.append((ds_id, f"L_K{m:04d}", sg, "RAZORPAY_CLEARING", "CR", c, None,
                            p["payment_id"], None, sid, None, _d(s["settlement_date"]), "settlement"))
    copy_rows(conn, "ledger_entries",
              ["dataset_id","ledger_entry_id","entry_group_id","account","direction","amount_paise",
               "order_id","payment_id","refund_id","settlement_id","seller_id","ledger_date",
               "description"], led)
    conn.commit()
    return ds_id
