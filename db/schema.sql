-- =============================================================================
-- Razor Recon AI -- P0 schema (PostgreSQL 15+)
--
-- HARD RULES ENCODED HERE:
--   * all money is BIGINT paise, column names end in _paise. No float/numeric.
--   * all rates are integer basis points.
--   * settlement_items is the source of truth; `settlements` is a rollup.
--   * composite PKs (dataset_id, <id>) so multiple seeded datasets coexist.
--   * derived rows carry run_id; runs are immutable.
-- =============================================================================

DROP TABLE IF EXISTS agent_transcripts, audit_log, exceptions, match_candidates, attributions,
    reconciliation_deltas, reconciliation_runs, money_edges,
    ground_truth_anomalies, ledger_entries, bank_transactions, settlement_items,
    settlements, adjustments, transfers, seller_allocations, refunds, payments,
    orders, sellers, customers, datasets CASCADE;

-- ---------------------------------------------------------------- control ---
CREATE TABLE datasets (
    dataset_id      UUID        PRIMARY KEY,
    seed            BIGINT      NOT NULL,
    policy_version  TEXT        NOT NULL,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_counts      JSONB       NOT NULL,
    label           TEXT
);

CREATE TABLE reconciliation_runs (
    run_id          UUID        PRIMARY KEY,
    dataset_id      UUID        NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    policy_version  TEXT        NOT NULL,
    engine_version  TEXT        NOT NULL,
    config_hash     TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'RUNNING'
                    CHECK (status IN ('RUNNING','COMPLETED','FAILED')),
    metrics         JSONB
);

-- ------------------------------------------------------------ source data ---
CREATE TABLE customers (
    dataset_id   UUID        NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    customer_id  TEXT        NOT NULL,
    name         TEXT        NOT NULL,
    email        TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (dataset_id, customer_id)
);

CREATE TABLE sellers (
    dataset_id      UUID  NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    seller_id       TEXT  NOT NULL,
    seller_name     TEXT  NOT NULL,
    seller_type     TEXT  NOT NULL
                    CHECK (seller_type IN ('INDIVIDUAL','SMB','ENTERPRISE')),
    commission_bps  INT   NOT NULL CHECK (commission_bps BETWEEN 0 AND 10000),
    status          TEXT  NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED')),
    PRIMARY KEY (dataset_id, seller_id)
);

-- NOTE: no seller_id on orders. A marketplace order may span multiple sellers;
-- seller attribution lives in seller_allocations, which is authoritative.
CREATE TABLE orders (
    dataset_id         UUID    NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    order_id           TEXT    NOT NULL,
    customer_id        TEXT    NOT NULL,
    order_amount_paise BIGINT  NOT NULL CHECK (order_amount_paise > 0),
    currency           CHAR(3) NOT NULL DEFAULT 'INR',
    order_date         DATE    NOT NULL,
    order_status       TEXT    NOT NULL
                       CHECK (order_status IN
                         ('CREATED','PAID','PARTIALLY_REFUNDED','REFUNDED','CANCELLED')),
    PRIMARY KEY (dataset_id, order_id),
    FOREIGN KEY (dataset_id, customer_id) REFERENCES customers(dataset_id, customer_id)
);

CREATE TABLE payments (
    dataset_id     UUID        NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    payment_id     TEXT        NOT NULL,
    order_id       TEXT        NOT NULL,
    customer_id    TEXT        NOT NULL,
    amount_paise   BIGINT      NOT NULL CHECK (amount_paise > 0),
    currency       CHAR(3)     NOT NULL DEFAULT 'INR',
    payment_status TEXT        NOT NULL
                   CHECK (payment_status IN
                     ('CREATED','AUTHORIZED','CAPTURED','FAILED','REFUNDED')),
    payment_method TEXT        NOT NULL
                   CHECK (payment_method IN
                     ('UPI','CARD','CARD_INTL','NETBANKING','WALLET')),
    created_at     TIMESTAMPTZ NOT NULL,
    captured_at    TIMESTAMPTZ,
    failure_reason TEXT,
    PRIMARY KEY (dataset_id, payment_id),
    FOREIGN KEY (dataset_id, order_id) REFERENCES orders(dataset_id, order_id),
    CONSTRAINT captured_iff_terminal CHECK (
        (payment_status IN ('CAPTURED','REFUNDED') AND captured_at IS NOT NULL)
        OR (payment_status NOT IN ('CAPTURED','REFUNDED') AND captured_at IS NULL)
    ),
    CONSTRAINT failure_reason_iff_failed CHECK (
        (payment_status = 'FAILED') = (failure_reason IS NOT NULL)
    )
);

-- Invariant enforced in the engine, not the DDL (INV-B2):
--   SUM(refund_amount_paise WHERE status='PROCESSED') <= payments.amount_paise
CREATE TABLE refunds (
    dataset_id          UUID   NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    refund_id           TEXT   NOT NULL,
    payment_id          TEXT   NOT NULL,
    refund_amount_paise BIGINT NOT NULL CHECK (refund_amount_paise > 0),
    refund_status       TEXT   NOT NULL
                        CHECK (refund_status IN ('PENDING','PROCESSED','FAILED')),
    refund_date         DATE   NOT NULL,
    refund_reason       TEXT,
    PRIMARY KEY (dataset_id, refund_id),
    FOREIGN KEY (dataset_id, payment_id) REFERENCES payments(dataset_id, payment_id)
);

CREATE TABLE seller_allocations (
    dataset_id            UUID   NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    allocation_id         TEXT   NOT NULL,
    payment_id            TEXT   NOT NULL,
    seller_id             TEXT   NOT NULL,
    gross_allocated_paise BIGINT NOT NULL CHECK (gross_allocated_paise > 0),
    commission_paise      BIGINT NOT NULL CHECK (commission_paise >= 0),
    net_seller_paise      BIGINT NOT NULL CHECK (net_seller_paise >= 0),
    allocation_status     TEXT   NOT NULL
                          CHECK (allocation_status IN ('PENDING','SETTLED','REVERSED')),
    allocation_date       DATE   NOT NULL,
    PRIMARY KEY (dataset_id, allocation_id),
    FOREIGN KEY (dataset_id, payment_id) REFERENCES payments(dataset_id, payment_id),
    FOREIGN KEY (dataset_id, seller_id)  REFERENCES sellers(dataset_id, seller_id),
    CONSTRAINT net_equals_gross_minus_commission
        CHECK (net_seller_paise = gross_allocated_paise - commission_paise)
);

-- allocation = intended split (a promise). transfer = money that actually moved.
-- Divergence between them is a first-class exception (Delta-4).
CREATE TABLE transfers (
    dataset_id         UUID   NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    transfer_id        TEXT   NOT NULL,
    payment_id         TEXT   NOT NULL,
    seller_id          TEXT   NOT NULL,
    amount_paise       BIGINT NOT NULL CHECK (amount_paise > 0),
    transfer_status    TEXT   NOT NULL
                       CHECK (transfer_status IN ('PENDING','PROCESSED','REVERSED','FAILED')),
    transfer_date      DATE   NOT NULL,
    transfer_reference TEXT,
    PRIMARY KEY (dataset_id, transfer_id),
    FOREIGN KEY (dataset_id, payment_id) REFERENCES payments(dataset_id, payment_id),
    FOREIGN KEY (dataset_id, seller_id)  REFERENCES sellers(dataset_id, seller_id)
);

CREATE TABLE adjustments (
    dataset_id      UUID        NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    adjustment_id   TEXT        NOT NULL,
    settlement_id   TEXT        NOT NULL,
    adjustment_type TEXT        NOT NULL
                    CHECK (adjustment_type IN
                      ('CHARGEBACK','CHARGEBACK_REVERSAL','FEE_CORRECTION',
                       'ROLLING_RESERVE_HOLD','ROLLING_RESERVE_RELEASE','MANUAL')),
    amount_paise    BIGINT      NOT NULL,          -- SIGNED: negative = deduction
    reason          TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    status          TEXT        NOT NULL CHECK (status IN ('APPLIED','REVERSED')),
    ref_payment_id  TEXT,                          -- set for FEE_CORRECTION: which payment it corrects
    PRIMARY KEY (dataset_id, adjustment_id)
);

CREATE TABLE settlements (
    dataset_id                  UUID   NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    settlement_id               TEXT   NOT NULL,
    settlement_date             DATE   NOT NULL,
    settlement_period_start     DATE   NOT NULL,
    settlement_period_end       DATE   NOT NULL,
    -- ROLLUP FIELDS. settlement_items is the source of truth; header != items
    -- is an exception (INV-B6), never an assumption.
    gross_amount_paise          BIGINT NOT NULL,
    refund_amount_paise         BIGINT NOT NULL,
    fee_amount_paise            BIGINT NOT NULL,
    tax_amount_paise            BIGINT NOT NULL,
    adjustment_amount_paise     BIGINT NOT NULL,    -- SIGNED
    net_settlement_amount_paise BIGINT NOT NULL,    -- what the gateway says it paid
    settlement_status           TEXT   NOT NULL
                                CHECK (settlement_status IN ('PROCESSED','ON_HOLD','FAILED')),
    settlement_utr              TEXT,               -- NULL when ON_HOLD or missing
    PRIMARY KEY (dataset_id, settlement_id),
    CHECK (settlement_period_start <= settlement_period_end),
    CHECK (settlement_date >= settlement_period_end)
);

-- TRANSFER TREATMENT -- stated explicitly to prevent double counting:
-- A TRANSFER row records that Route moved money to a seller's linked account.
-- It is a SEPARATE money movement from the merchant's own settlement and is
-- NEVER included in gross/refund/adjustment sums for Delta-1. It exists here
-- purely so money_edges can trace "this transfer happened inside the window
-- covered by settlement S". Its correctness is checked by Delta-4.
CREATE TABLE settlement_items (
    dataset_id          UUID   NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    settlement_item_id  TEXT   NOT NULL,
    settlement_id       TEXT   NOT NULL,
    transaction_type    TEXT   NOT NULL
                        CHECK (transaction_type IN
                          ('PAYMENT','REFUND','ADJUSTMENT','TRANSFER')),
    payment_id          TEXT,
    refund_id           TEXT,
    adjustment_id       TEXT,
    transfer_id         TEXT,
    amount_paise        BIGINT NOT NULL,   -- SIGNED: PAYMENT +, REFUND -, ADJUSTMENT signed
    fee_paise           BIGINT NOT NULL DEFAULT 0,
    tax_paise           BIGINT NOT NULL DEFAULT 0,
    transaction_date    DATE   NOT NULL,
    PRIMARY KEY (dataset_id, settlement_item_id),
    FOREIGN KEY (dataset_id, settlement_id) REFERENCES settlements(dataset_id, settlement_id),
    CONSTRAINT exactly_one_reference CHECK (
        (CASE WHEN payment_id    IS NOT NULL THEN 1 ELSE 0 END +
         CASE WHEN refund_id     IS NOT NULL THEN 1 ELSE 0 END +
         CASE WHEN adjustment_id IS NOT NULL THEN 1 ELSE 0 END +
         CASE WHEN transfer_id   IS NOT NULL THEN 1 ELSE 0 END) = 1
    )
);

CREATE TABLE bank_transactions (
    dataset_id          UUID    NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    bank_transaction_id TEXT    NOT NULL,
    transaction_date    DATE    NOT NULL,
    description         TEXT    NOT NULL,   -- free text, UNTRUSTED
    credit_paise        BIGINT  NOT NULL DEFAULT 0 CHECK (credit_paise >= 0),
    debit_paise         BIGINT  NOT NULL DEFAULT 0 CHECK (debit_paise  >= 0),
    currency            CHAR(3) NOT NULL DEFAULT 'INR',
    bank_reference      TEXT    NOT NULL,
    settlement_utr      TEXT,               -- frequently NULL. That is the point.
    PRIMARY KEY (dataset_id, bank_transaction_id),
    CONSTRAINT one_sided CHECK (
        (credit_paise = 0 AND debit_paise > 0) OR
        (debit_paise  = 0 AND credit_paise > 0)
    )
);

CREATE TABLE ledger_entries (
    dataset_id      UUID   NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    ledger_entry_id TEXT   NOT NULL,
    entry_group_id  TEXT   NOT NULL,     -- groups the legs of one double-entry txn
    account         TEXT   NOT NULL
                    CHECK (account IN
                      ('BANK','RAZORPAY_CLEARING','SALES','GATEWAY_FEES',
                       'INPUT_GST','REFUNDS','SELLER_PAYABLE')),
    direction       TEXT   NOT NULL CHECK (direction IN ('DR','CR')),
    amount_paise    BIGINT NOT NULL CHECK (amount_paise > 0),
    order_id        TEXT,
    payment_id      TEXT,
    refund_id       TEXT,
    settlement_id   TEXT,
    seller_id       TEXT,
    ledger_date     DATE   NOT NULL,
    description     TEXT,
    PRIMARY KEY (dataset_id, ledger_entry_id)
);

-- ---------------------------------------------------------------- lineage ---
CREATE TABLE money_edges (
    dataset_id   UUID   NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    src_type     TEXT   NOT NULL,
    src_id       TEXT   NOT NULL,
    dst_type     TEXT   NOT NULL,
    dst_id       TEXT   NOT NULL,
    edge_kind    TEXT   NOT NULL,
    amount_paise BIGINT,
    PRIMARY KEY (dataset_id, src_type, src_id, dst_type, dst_id, edge_kind)
);

-- ----------------------------------------------------------- ground truth ---
CREATE TABLE ground_truth_anomalies (
    dataset_id              UUID   NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    anomaly_id              TEXT   NOT NULL,
    anomaly_type            TEXT   NOT NULL,
    subject_type            TEXT   NOT NULL,
    subject_id              TEXT   NOT NULL,
    settlement_id           TEXT,
    expected_delta_kind     TEXT,
    expected_exception_type TEXT,
    original_field          TEXT,
    original_value_paise    BIGINT,
    mutated_value_paise     BIGINT,
    planted_amount_paise    BIGINT NOT NULL,
    is_resolvable           BOOLEAN NOT NULL,
    notes                   TEXT,
    PRIMARY KEY (dataset_id, anomaly_id)
);

-- --------------------------------------------------------- derived/result ---
CREATE TABLE reconciliation_deltas (
    run_id          UUID   NOT NULL REFERENCES reconciliation_runs(run_id) ON DELETE CASCADE,
    delta_id        TEXT   NOT NULL,
    settlement_id   TEXT   NOT NULL,
    delta_kind      TEXT   NOT NULL
                    CHECK (delta_kind IN ('D1_COMPUTE','D2_BANK','D3_LEDGER','D4_PAYOUT')),
    subject_id      TEXT,                  -- allocation_id for D4, else NULL
    expected_paise  BIGINT NOT NULL,
    actual_paise    BIGINT NOT NULL,
    delta_paise     BIGINT NOT NULL,       -- expected - actual
    explained_paise BIGINT NOT NULL DEFAULT 0,
    residual_paise  BIGINT NOT NULL,
    tier            CHAR(1) CHECK (tier IN ('A','B','C')),
    status          TEXT   NOT NULL
                    CHECK (status IN ('MATCHED','EXPLAINED','REVIEW','UNRESOLVED')),
    PRIMARY KEY (run_id, delta_id)
);

-- The UNIQUE constraint is the double-counting guard.
CREATE TABLE attributions (
    run_id              UUID   NOT NULL REFERENCES reconciliation_runs(run_id) ON DELETE CASCADE,
    attribution_id      TEXT   NOT NULL,
    delta_id            TEXT   NOT NULL,
    evidence_type       TEXT   NOT NULL,
    evidence_record_id  TEXT   NOT NULL,
    signed_amount_paise BIGINT NOT NULL,
    derivation          TEXT   NOT NULL
                        CHECK (derivation IN ('DETERMINISTIC','FUZZY')),
    rule_ids            TEXT[] NOT NULL DEFAULT '{}',
    rationale           TEXT   NOT NULL,
    PRIMARY KEY (run_id, attribution_id),
    UNIQUE (run_id, delta_id, evidence_type, evidence_record_id)
);

CREATE TABLE match_candidates (
    run_id              UUID    NOT NULL REFERENCES reconciliation_runs(run_id) ON DELETE CASCADE,
    settlement_id       TEXT    NOT NULL,
    bank_transaction_id TEXT    NOT NULL,
    pass_name           TEXT    NOT NULL,
    score_bps           INT     NOT NULL,
    is_selected         BOOLEAN NOT NULL DEFAULT FALSE,
    is_ambiguous        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (run_id, settlement_id, bank_transaction_id, pass_name)
);

CREATE TABLE exceptions (
    run_id             UUID        NOT NULL REFERENCES reconciliation_runs(run_id) ON DELETE CASCADE,
    exception_id       TEXT        NOT NULL,
    settlement_id      TEXT        NOT NULL,
    subject_id         TEXT,
    delta_kind         TEXT        NOT NULL,
    exception_type     TEXT        NOT NULL,
    severity           TEXT        NOT NULL CHECK (severity IN ('LOW','MEDIUM','HIGH')),
    amount_paise       BIGINT      NOT NULL,
    explained_paise    BIGINT      NOT NULL,
    unexplained_paise  BIGINT      NOT NULL,
    tier               CHAR(1)     NOT NULL,
    status             TEXT        NOT NULL
                       CHECK (status IN ('AUTO_RESOLVED','NEEDS_REVIEW','UNRESOLVED')),
    recommended_action TEXT        NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, exception_id)
);

CREATE TABLE audit_log (
    run_id       UUID        NOT NULL REFERENCES reconciliation_runs(run_id) ON DELETE CASCADE,
    audit_id     BIGINT      NOT NULL,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor        TEXT        NOT NULL CHECK (actor IN ('ENGINE','HUMAN')),
    action       TEXT        NOT NULL,
    subject_type TEXT        NOT NULL,
    subject_id   TEXT        NOT NULL,
    inputs       JSONB,
    rule_ids     TEXT[],
    outputs      JSONB,
    decision     TEXT,
    tier         CHAR(1),
    PRIMARY KEY (run_id, audit_id)
);


-- ---------------------------------------------------------------------------
-- Agent transcripts. The investigation agent explains results; it never writes
-- to any table above. This is its own record: what was asked, what it answered,
-- which tools it called and whether every id it cited actually appeared in the
-- evidence it read. Kept beside the engine's audit_log so a reviewer can hold
-- the deterministic decision trail and the narrated one side by side.
--
-- Mirrored in db/agent.sql, which is idempotent and safe to apply to a LIVE
-- database. Use that one to add the agent to an existing install -- reloading
-- this file would drop every table and take the dataset with it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_transcripts (
    run_id                 UUID        NOT NULL REFERENCES reconciliation_runs(run_id)
                                       ON DELETE CASCADE,
    turn_id                BIGINT      NOT NULL,
    asked_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    question               TEXT        NOT NULL,
    answer                 TEXT        NOT NULL,
    provider               TEXT,
    model                  TEXT,
    tool_calls             JSONB       NOT NULL DEFAULT '[]',
    tool_call_count        INT         NOT NULL DEFAULT 0,
    citations              TEXT[]      NOT NULL DEFAULT '{}',
    unsupported_references TEXT[]      NOT NULL DEFAULT '{}',
    grounded               BOOLEAN     NOT NULL DEFAULT TRUE,
    stop_reason            TEXT,
    elapsed_seconds        NUMERIC(8,2),
    PRIMARY KEY (run_id, turn_id)
);
CREATE INDEX IF NOT EXISTS agent_transcript_run_idx ON agent_transcripts (run_id, turn_id);
