BEGIN;

CREATE TABLE users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT NOT NULL UNIQUE,
    kyc_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (kyc_status IN ('pending', 'verified', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    name TEXT NOT NULL,
    account_type TEXT NOT NULL
        CHECK (account_type IN ('wallet', 'cash_in', 'cash_out', 'fee_revenue')),
    currency CHAR(3) NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
    balance_paise BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((account_type = 'wallet' AND user_id IS NOT NULL)
        OR (account_type <> 'wallet' AND user_id IS NULL)),
    CHECK (account_type <> 'wallet' OR balance_paise >= 0)
);
CREATE INDEX accounts_user_idx ON accounts(user_id, id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX one_system_account_per_type_idx
    ON accounts(account_type) WHERE user_id IS NULL;

CREATE TABLE transfers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_type TEXT NOT NULL
        CHECK (transfer_type IN ('top_up', 'peer', 'withdrawal', 'fee', 'reversal')),
    status TEXT NOT NULL
        CHECK (status IN ('processing', 'completed', 'held', 'rejected', 'failed')),
    amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
    note TEXT NOT NULL DEFAULT '',
    sender_account_id BIGINT NOT NULL REFERENCES accounts(id),
    recipient_account_id BIGINT NOT NULL REFERENCES accounts(id),
    reversal_of BIGINT REFERENCES transfers(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CHECK (sender_account_id <> recipient_account_id)
);
CREATE INDEX transfers_created_idx ON transfers(created_at DESC, id DESC);
CREATE INDEX transfers_status_created_idx ON transfers(status, created_at);
CREATE INDEX transfers_sender_recent_idx ON transfers(sender_account_id, status, created_at DESC);
CREATE INDEX transfers_sender_recipient_idx
    ON transfers(sender_account_id, recipient_account_id, status);
CREATE UNIQUE INDEX one_reversal_per_transfer_idx ON transfers(reversal_of) WHERE reversal_of IS NOT NULL;

CREATE TABLE ledger_entries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id BIGINT NOT NULL REFERENCES transfers(id),
    account_id BIGINT NOT NULL REFERENCES accounts(id),
    direction TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ledger_account_statement_idx
    ON ledger_entries(account_id, created_at DESC, id DESC);
CREATE INDEX ledger_transfer_idx ON ledger_entries(transfer_id);

CREATE FUNCTION prevent_ledger_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger entries are append-only';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER ledger_entries_immutable
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION prevent_ledger_mutation();

CREATE TABLE idempotency_keys (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    idempotency_key UUID NOT NULL,
    request_hash CHAR(64) NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'completed')),
    response_code INTEGER,
    response_body JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (status = 'processing' AND response_code IS NULL AND response_body IS NULL)
        OR (status = 'completed' AND response_code IS NOT NULL AND response_body IS NOT NULL)
    ),
    UNIQUE(user_id, idempotency_key)
);
CREATE UNIQUE INDEX idempotency_anonymous_key_idx
    ON idempotency_keys(idempotency_key) WHERE user_id IS NULL;

CREATE TABLE transfer_status_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id BIGINT NOT NULL REFERENCES transfers(id),
    old_status TEXT,
    new_status TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX transfer_audit_transfer_idx ON transfer_status_audit(transfer_id, changed_at);

CREATE FUNCTION audit_transfer_status_change() RETURNS trigger AS $$
DECLARE
    actor TEXT;
BEGIN
    actor := COALESCE(NULLIF(current_setting('app.changed_by', TRUE), ''), 'system');
    IF TG_OP = 'INSERT' THEN
        INSERT INTO transfer_status_audit(transfer_id, old_status, new_status, changed_by)
        VALUES (NEW.id, NULL, NEW.status, actor);
    ELSIF OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO transfer_status_audit(transfer_id, old_status, new_status, changed_by)
        VALUES (NEW.id, OLD.status, NEW.status, actor);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER transfers_status_audit_insert
    AFTER INSERT ON transfers
    FOR EACH ROW EXECUTE FUNCTION audit_transfer_status_change();
CREATE TRIGGER transfers_status_audit_update
    AFTER UPDATE OF status ON transfers
    FOR EACH ROW EXECUTE FUNCTION audit_transfer_status_change();

CREATE TABLE risk_rules (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    rule_type TEXT NOT NULL UNIQUE
        CHECK (rule_type IN ('velocity', 'amount_spike', 'new_payee')),
    threshold BIGINT NOT NULL CHECK (threshold > 0),
    window_minutes INTEGER NOT NULL DEFAULT 0 CHECK (window_minutes >= 0),
    action TEXT NOT NULL CHECK (action IN ('hold', 'flag_allow')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE
);
INSERT INTO risk_rules(name, rule_type, threshold, window_minutes, action) VALUES
    ('Velocity', 'velocity', 5, 10, 'hold'),
    ('Amount spike', 'amount_spike', 5, 0, 'hold'),
    ('New payee, large amount', 'new_payee', 1000000, 0, 'flag_allow');

CREATE TABLE transfer_risk_flags (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id BIGINT NOT NULL REFERENCES transfers(id),
    rule_id BIGINT NOT NULL REFERENCES risk_rules(id),
    explanation TEXT,
    label TEXT CHECK (label IN ('likely_ok', 'review', 'likely_fraud')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(transfer_id, rule_id)
);
CREATE INDEX unexplained_risk_flags_idx
    ON transfer_risk_flags(id)
    WHERE explanation IS NULL;

CREATE TABLE reconciliation_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'passed', 'issues')),
    problem_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE reconciliation_problems (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES reconciliation_runs(id),
    check_name TEXT NOT NULL,
    entity_id BIGINT,
    expected TEXT,
    actual TEXT
);
CREATE TABLE job_locks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name TEXT NOT NULL UNIQUE
);
INSERT INTO job_locks(job_name) VALUES ('nightly_reconciliation'), ('risk_explanations');

CREATE TABLE statement_answers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    question TEXT NOT NULL,
    answer JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE schema_migrations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES ('001_init');

INSERT INTO accounts(user_id, name, account_type) VALUES
    (NULL, 'Cash-in clearing', 'cash_in'),
    (NULL, 'Cash-out clearing', 'cash_out'),
    (NULL, 'Fee revenue', 'fee_revenue');

CREATE FUNCTION validate_transfer_balance() RETURNS trigger AS $$
DECLARE
    target_transfer BIGINT;
    transfer_status TEXT;
    transfer_amount BIGINT;
    debit_total BIGINT;
    credit_total BIGINT;
    entry_count BIGINT;
BEGIN
    target_transfer := COALESCE(NEW.transfer_id, OLD.transfer_id);
    SELECT status, amount_paise
      INTO transfer_status, transfer_amount
      FROM transfers WHERE id = target_transfer;
    IF transfer_status IN ('completed', 'reversal') THEN
        SELECT COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'debit'), 0),
               COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'credit'), 0),
               COUNT(*)
          INTO debit_total, credit_total, entry_count
          FROM ledger_entries WHERE transfer_id = target_transfer;
        IF entry_count < 2 OR debit_total <> credit_total OR debit_total <> transfer_amount THEN
            RAISE EXCEPTION 'transfer % ledger entries are not balanced', target_transfer;
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER ledger_transfer_balanced
    AFTER INSERT OR UPDATE OR DELETE ON ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_transfer_balance();

CREATE FUNCTION validate_completed_transfer() RETURNS trigger AS $$
DECLARE
    debit_total BIGINT;
    credit_total BIGINT;
    entry_count BIGINT;
BEGIN
    IF NEW.status = 'completed' THEN
        SELECT COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'debit'), 0),
               COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'credit'), 0),
               COUNT(*)
          INTO debit_total, credit_total, entry_count
          FROM ledger_entries WHERE transfer_id = NEW.id;
        IF entry_count < 2 OR debit_total <> credit_total OR debit_total <> NEW.amount_paise THEN
            RAISE EXCEPTION 'completed transfer % has invalid ledger entries', NEW.id;
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER completed_transfer_has_balanced_entries
    AFTER INSERT OR UPDATE ON transfers
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_completed_transfer();

COMMIT;
