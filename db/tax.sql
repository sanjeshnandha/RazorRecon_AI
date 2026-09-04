-- ============================================================================
-- Tax-line matcher: the third source of truth.
-- ============================================================================
-- Idempotent, like db/agent.sql. This installs onto a LIVE database without
-- touching a single existing row or column -- schema.sql is destructive and
-- must not be re-run to get this feature.
--
--   make tax-schema      (or)     psql "$DATABASE_URL" -f db/tax.sql
--
-- What it holds: the merchant's GSTR-2B lines. GSTR-2B is the statement the GST
-- portal auto-drafts each month from what a business's SUPPLIERS filed. It is
-- not the merchant's own record and not the gateway's settlement report -- it is
-- the third, independent answer to "how much input tax credit can be claimed",
-- and it is the only one that decides whether the money is actually recoverable.
--
-- Nothing in the reconciliation path reads this table. The four deltas, the
-- tier gate and every accuracy number are computed exactly as before.
-- ============================================================================

CREATE TABLE IF NOT EXISTS tax_invoices (
    dataset_id          UUID   NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    invoice_no          TEXT   NOT NULL,
    invoice_date        DATE   NOT NULL,
    -- The GSTR-2B return period the line APPEARED IN, 'YYYY-MM'. Distinct from
    -- invoice_date: a late filing puts an early invoice in a later period, and
    -- that gap is exactly what a timing check looks for.
    return_period       TEXT   NOT NULL,
    supplier_gstin      TEXT   NOT NULL,
    document_type       TEXT   NOT NULL DEFAULT 'INVOICE',
    settlement_id       TEXT,
    taxable_value_paise BIGINT NOT NULL,
    cgst_paise          BIGINT NOT NULL DEFAULT 0,
    sgst_paise          BIGINT NOT NULL DEFAULT 0,
    igst_paise          BIGINT NOT NULL DEFAULT 0,
    -- GSTR-2B carries an "ITC availability" flag per line. A line can match on
    -- every rupee and still be unclaimable.
    itc_eligible        BOOLEAN NOT NULL DEFAULT TRUE,
    ineligible_reason   TEXT,
    filed_at            DATE,
    PRIMARY KEY (dataset_id, invoice_no),
    CONSTRAINT tax_invoices_document_type_check
        CHECK (document_type IN ('INVOICE', 'CREDIT_NOTE')),
    CONSTRAINT tax_invoices_return_period_check
        CHECK (return_period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    -- Integer paise, like every other money column in this schema.
    CONSTRAINT tax_invoices_amounts_check
        CHECK (taxable_value_paise >= 0 AND cgst_paise >= 0
               AND sgst_paise >= 0 AND igst_paise >= 0),
    -- A supply is either intra-state (CGST+SGST) or inter-state (IGST). Never
    -- both. A row carrying both is malformed, not an anomaly to be diagnosed.
    CONSTRAINT tax_invoices_split_check
        CHECK ((igst_paise = 0) OR (cgst_paise = 0 AND sgst_paise = 0)),
    CONSTRAINT tax_invoices_reason_iff_ineligible
        CHECK (itc_eligible = (ineligible_reason IS NULL))
);

CREATE INDEX IF NOT EXISTS tax_inv_settlement_idx
    ON tax_invoices (dataset_id, settlement_id);
CREATE INDEX IF NOT EXISTS tax_inv_period_idx
    ON tax_invoices (dataset_id, return_period);
