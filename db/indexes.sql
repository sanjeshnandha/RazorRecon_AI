-- Indexes sized for the engine's actual access patterns: everything is
-- "give me all X for this dataset", so dataset_id leads every one of them.
CREATE INDEX IF NOT EXISTS ledger_group_idx     ON ledger_entries (dataset_id, entry_group_id);
CREATE INDEX IF NOT EXISTS ledger_payment_idx   ON ledger_entries (dataset_id, payment_id);
CREATE INDEX IF NOT EXISTS ledger_settle_idx    ON ledger_entries (dataset_id, settlement_id);
CREATE INDEX IF NOT EXISTS money_edges_fwd      ON money_edges (dataset_id, src_type, src_id);
CREATE INDEX IF NOT EXISTS money_edges_rev      ON money_edges (dataset_id, dst_type, dst_id);
CREATE INDEX IF NOT EXISTS si_settlement_idx    ON settlement_items (dataset_id, settlement_id);
CREATE INDEX IF NOT EXISTS si_payment_idx       ON settlement_items (dataset_id, payment_id);
CREATE INDEX IF NOT EXISTS refunds_payment_idx  ON refunds (dataset_id, payment_id);
CREATE INDEX IF NOT EXISTS alloc_payment_idx    ON seller_allocations (dataset_id, payment_id);
CREATE INDEX IF NOT EXISTS alloc_seller_idx     ON seller_allocations (dataset_id, seller_id);
CREATE INDEX IF NOT EXISTS transfers_ps_idx     ON transfers (dataset_id, payment_id, seller_id);
CREATE INDEX IF NOT EXISTS adj_settlement_idx   ON adjustments (dataset_id, settlement_id);
CREATE INDEX IF NOT EXISTS bank_date_idx        ON bank_transactions (dataset_id, transaction_date);
CREATE INDEX IF NOT EXISTS bank_utr_idx         ON bank_transactions (dataset_id, settlement_utr);
CREATE INDEX IF NOT EXISTS settle_date_idx      ON settlements (dataset_id, settlement_date);
CREATE INDEX IF NOT EXISTS deltas_settle_idx    ON reconciliation_deltas (run_id, settlement_id);
CREATE INDEX IF NOT EXISTS deltas_kind_idx      ON reconciliation_deltas (run_id, delta_kind);
CREATE INDEX IF NOT EXISTS attr_delta_idx       ON attributions (run_id, delta_id);
CREATE INDEX IF NOT EXISTS exc_settle_idx       ON exceptions (run_id, settlement_id);
CREATE INDEX IF NOT EXISTS exc_status_idx       ON exceptions (run_id, status);
CREATE INDEX IF NOT EXISTS gt_settle_idx        ON ground_truth_anomalies (dataset_id, settlement_id);
