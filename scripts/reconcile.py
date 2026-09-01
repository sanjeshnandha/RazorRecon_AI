"""
Run the engine over a dataset and print the report a judge should see first.

    python -m scripts.reconcile                 # newest dataset
    python -m scripts.reconcile <dataset_id>
"""
from __future__ import annotations

import sys

from engine import runner
from engine.db import connect, fetch_one
from engine.money import rupees


def line(char="-", n=78):
    print(char * n)


def main() -> int:
    with connect() as conn:
        if len(sys.argv) > 1:
            dataset_id = sys.argv[1]
        else:
            row = fetch_one(conn, "SELECT dataset_id FROM datasets ORDER BY generated_at DESC LIMIT 1")
            if not row:
                print("No datasets. Run:  make generate")
                return 1
            dataset_id = str(row["dataset_id"])
        m = runner.run(conn, dataset_id)

    t, a, g, ti, ex = (m["throughput"], m["accuracy"], m["ground_truth"], m["tiers"],
                       m["exceptions"])
    line("=")
    print("  AI FINANCE CONTROLLER -- reconciliation report")
    print(f"  run {m['run_id']}   policy {m['policy_version']}   config {m['config_hash']}")
    line("=")
    print()
    print("THROUGHPUT")
    print("  " + t["headline"])
    print(f"  batch completed in {t['elapsed_seconds']}s "
          f"({t['records_per_second']:,} records/s); "
          f"p50 {t['p50_settlement_ms']}ms, p95 {t['p95_settlement_ms']}ms per settlement")
    print()
    print("MEASURED ACCURACY -- reported separately, never blended")
    print(f"  settlement match rate (all four deltas) {a['record_match_rate_pct']:>8.2f}%"
          f"   {a['settlements_fully_resolved']}/{t['settlements']}")
    print(f"  monetary reconciliation rate            {a['monetary_reconciliation_rate_pct']:>8.2f}%"
          f"   of {rupees(a['total_settled_value_paise'])} settled value")
    print(f"  seller payout reconciliation (D4)       "
          f"{a['seller_payout_reconciliation_rate_pct']:>8.2f}%")
    print(f"    D1 compute {a['d1_match_rate_pct']:>6.2f}%   D2 bank {a['d2_match_rate_pct']:>6.2f}%"
          f"   D3 ledger {a['d3_match_rate_pct']:>6.2f}%   D4 payout "
          f"{a['d4_match_rate_pct']:>6.2f}%")
    print()
    print("AMOUNT AT RISK, RIGHT NOW")
    print(f"  {a['amount_at_risk_display']}   across {ex['open']} open exceptions")
    print()
    print("HONESTY CHECKS (scored against ground truth planted at generation time)")
    print(f"  resolvable anomalies detected      {g['resolvable_detected']:>3}/"
          f"{g['resolvable_planted']:<3} {g['resolvable_detection_rate_pct']:>7.2f}%")
    print(f"  diagnosis accuracy                 {g['diagnosis_correct']:>3}/"
          f"{g['resolvable_planted']:<3} {g['diagnosis_accuracy_pct']:>7.2f}%")
    print(f"  undiagnosable cases escalated      "
          f"{g['unresolvable_correctly_escalated']:>3}/{g['unresolvable_planted']:<3} "
          f"{g['correct_escalation_rate_pct']:>7.2f}%")
    print(f"  false-positive traps avoided       {g['false_positive_traps_avoided']:>3}/"
          f"{g['false_positive_traps']:<3}")
    print(f"  FALSE AUTO-RESOLUTIONS             {g['false_auto_resolution_count']:>3}"
          f"      (must be 0)")
    for c in g["matcher_guard_checks"]:
        print(f"    guard [{'held' if c['passed'] else 'BROKE'}] {c['settlement_id']}: "
              f"{c['guard']} -> resolved by {c['matched_by'] or 'nothing (tier C)'}")
    print()
    print("TIERS")
    print(f"  A auto-resolved {ti['tier_a']:>5} ({ti['auto_resolution_rate_pct']:.2f}%)   "
          f"B needs review {ti['tier_b']:>4} ({ti['human_review_rate_pct']:.2f}%)   "
          f"C unresolved {ti['tier_c']:>4} ({ti['unresolved_rate_pct']:.2f}%)")
    print()
    print("EXCEPTIONS BY TYPE")
    for k, v in sorted(ex["by_type"].items(), key=lambda x: -x[1]):
        print(f"  {v:>3}  {k}")
    print()
    line()
    print("  " + m["demo_policy_disclaimer"])
    line()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
