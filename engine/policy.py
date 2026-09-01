"""Policy registry loader. Single source of truth for the generator AND the engine."""
from __future__ import annotations

import hashlib
import os
from datetime import date
from functools import lru_cache
from typing import Any

import yaml

POLICY_PATH = os.environ.get(
    "POLICY_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "policy", "policy.yaml"),
)

DEMO_POLICY_LABEL = "Demo Merchant Policy - not Razorpay's actual terms"


class Policy:
    """Typed accessor over policy.yaml. Every getter can emit its rule ID."""

    def __init__(self, raw: dict[str, Any], raw_text: str):
        self.raw = raw
        self.version: str = raw["version"]
        self.currency: str = raw["currency"]
        self.rounding_mode: str = raw["rounding"]["mode"]
        self.tax_computation: str = raw["rounding"]["tax_computation"]
        self._mdr: dict[str, int] = {k: int(v) for k, v in raw["mdr_bps"].items()}
        self.gst_on_fee_bps: int = int(raw["gst_on_fee_bps"])
        self.refund_window_days: int = int(raw["refunds"]["window_days"])
        self.mdr_refunded: bool = bool(raw["refunds"]["mdr_refunded"])
        self.cycle_working_days: int = int(raw["settlement"]["cycle_working_days"])
        self.exclude_sundays: bool = bool(raw["settlement"]["exclude_sundays"])
        self.exclude_second_fourth_saturday: bool = bool(
            raw["settlement"]["exclude_second_fourth_saturday"]
        )
        self.holidays: frozenset[date] = frozenset(
            date.fromisoformat(d) for d in raw["settlement"]["holidays"]
        )
        self.expected_lag_days: int = int(raw["bank_credit"]["expected_lag_days"])
        self.bank_tolerance_days: int = int(raw["bank_credit"]["tolerance_days"])
        self._commission: dict[str, int] = {
            k: int(v) for k, v in raw["commission_bps_by_seller_type"].items()
        }
        m = raw["matching"]
        self.amount_tolerance_paise: int = int(m["amount_tolerance_paise"])
        self.date_window_days: int = int(m["date_window_days"])
        self.subset_sum_max_candidates: int = int(m["subset_sum_max_candidates"])
        self.subset_sum_max_subset_size: int = int(m["subset_sum_max_subset_size"])
        self.fuzzy_reference_min_score_bps: int = int(m["fuzzy_reference_min_score_bps"])
        self.config_hash: str = hashlib.sha256(raw_text.encode()).hexdigest()[:16]

    # --- rate lookups, each paired with its rule ID -------------------------
    def mdr_bps(self, payment_method: str) -> int:
        return self._mdr[payment_method]

    def mdr_rule(self, payment_method: str) -> str:
        return f"POLICY.MDR.{payment_method}@{self.version}"

    @property
    def gst_rule(self) -> str:
        return f"POLICY.TAX.GST_ON_FEE@{self.version}"

    def commission_bps(self, seller_type: str) -> int:
        return self._commission[seller_type]

    def commission_rule(self, seller_type: str) -> str:
        return f"POLICY.COMMISSION.{seller_type}@{self.version}"

    @property
    def refund_rule(self) -> str:
        return f"POLICY.REFUND.WINDOW@{self.version}"

    @property
    def settlement_rule(self) -> str:
        return f"POLICY.SETTLEMENT.CYCLE_WORKING_DAYS@{self.version}"

    @property
    def bank_rule(self) -> str:
        return f"POLICY.BANK.TOLERANCE_DAYS@{self.version}"

    def rule(self, section: str, key: str) -> str:
        return f"POLICY.{section}.{key}@{self.version}"


@lru_cache(maxsize=4)
def load_policy(path: str = POLICY_PATH) -> Policy:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return Policy(yaml.safe_load(text), text)
