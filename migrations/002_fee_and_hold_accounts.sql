BEGIN;

ALTER TABLE accounts
    DROP CONSTRAINT IF EXISTS accounts_account_type_check;
ALTER TABLE accounts
    ADD CONSTRAINT accounts_account_type_check
    CHECK (account_type IN ('wallet', 'cash_in', 'cash_out', 'fee_revenue', 'hold'));

INSERT INTO accounts(user_id, name, account_type)
VALUES (NULL, 'Transfer hold clearing', 'hold')
ON CONFLICT (account_type) WHERE user_id IS NULL DO NOTHING;

ALTER TABLE transfers
    ADD COLUMN IF NOT EXISTS fee_for_transfer_id BIGINT REFERENCES transfers(id);
CREATE UNIQUE INDEX IF NOT EXISTS one_fee_transfer_per_peer_idx
    ON transfers(fee_for_transfer_id)
    WHERE fee_for_transfer_id IS NOT NULL;
ALTER TABLE transfers
    DROP CONSTRAINT IF EXISTS fee_transfer_parent_type_check;
ALTER TABLE transfers
    ADD CONSTRAINT fee_transfer_parent_type_check
    CHECK (fee_for_transfer_id IS NULL OR transfer_type = 'fee');

CREATE OR REPLACE FUNCTION validate_transfer_balance() RETURNS trigger AS $$
DECLARE
    target_transfer BIGINT;
    transfer_status TEXT;
    transfer_amount BIGINT;
    debit_total BIGINT;
    credit_total BIGINT;
    entry_count BIGINT;
    hold_entry_count BIGINT;
BEGIN
    target_transfer := COALESCE(NEW.transfer_id, OLD.transfer_id);
    SELECT status, amount_paise
      INTO transfer_status, transfer_amount
      FROM transfers WHERE id = target_transfer;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'debit'), 0),
           COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'credit'), 0),
           COUNT(l.id),
           COUNT(l.id) FILTER (WHERE a.account_type = 'hold')
      INTO debit_total, credit_total, entry_count, hold_entry_count
      FROM ledger_entries l
      JOIN accounts a ON a.id = l.account_id
     WHERE l.transfer_id = target_transfer;

    IF entry_count < 2 OR debit_total <> credit_total THEN
        RAISE EXCEPTION 'transfer % ledger entries are not balanced', target_transfer;
    END IF;
    IF transfer_status = 'held' THEN
        IF hold_entry_count <> 1 OR entry_count <> 2 OR debit_total <> transfer_amount THEN
            RAISE EXCEPTION 'held transfer % must reserve its amount in the hold account', target_transfer;
        END IF;
    ELSIF hold_entry_count = 0 THEN
        IF debit_total <> transfer_amount THEN
            RAISE EXCEPTION 'transfer % ledger total does not match its amount', target_transfer;
        END IF;
    ELSIF hold_entry_count <> 2 OR entry_count <> 4 OR debit_total <> transfer_amount * 2 THEN
        RAISE EXCEPTION 'settled held transfer % has invalid ledger entries', target_transfer;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION validate_completed_transfer() RETURNS trigger AS $$
DECLARE
    debit_total BIGINT;
    credit_total BIGINT;
    entry_count BIGINT;
    hold_entry_count BIGINT;
BEGIN
    SELECT COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'debit'), 0),
           COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'credit'), 0),
           COUNT(l.id),
           COUNT(l.id) FILTER (WHERE a.account_type = 'hold')
      INTO debit_total, credit_total, entry_count, hold_entry_count
      FROM ledger_entries l
      JOIN accounts a ON a.id = l.account_id
     WHERE l.transfer_id = NEW.id;

    IF entry_count < 2 OR debit_total <> credit_total THEN
        RAISE EXCEPTION 'transfer % ledger entries are not balanced', NEW.id;
    END IF;
    IF NEW.status = 'held' THEN
        IF hold_entry_count <> 1 OR entry_count <> 2 OR debit_total <> NEW.amount_paise THEN
            RAISE EXCEPTION 'held transfer % must reserve its amount in the hold account', NEW.id;
        END IF;
    ELSIF hold_entry_count = 0 THEN
        IF debit_total <> NEW.amount_paise THEN
            RAISE EXCEPTION 'transfer % ledger total does not match its amount', NEW.id;
        END IF;
    ELSIF hold_entry_count <> 2 OR entry_count <> 4 OR debit_total <> NEW.amount_paise * 2 THEN
        RAISE EXCEPTION 'settled held transfer % has invalid ledger entries', NEW.id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    held_transfer RECORD;
    debited BIGINT;
    hold_account_id BIGINT;
BEGIN
    SELECT id INTO hold_account_id
    FROM accounts
    WHERE user_id IS NULL AND account_type = 'hold';

    FOR held_transfer IN
        SELECT t.id, t.sender_account_id, t.amount_paise
        FROM transfers t
        WHERE t.status = 'held'
          AND NOT EXISTS (
              SELECT 1 FROM ledger_entries l WHERE l.transfer_id = t.id
          )
        ORDER BY t.id
    LOOP
        UPDATE accounts
        SET balance_paise = balance_paise - held_transfer.amount_paise
        WHERE id = held_transfer.sender_account_id
          AND account_type = 'wallet'
          AND balance_paise >= held_transfer.amount_paise
        RETURNING id INTO debited;
        IF NOT FOUND THEN
            RAISE EXCEPTION
                'Cannot migrate held transfer %: sender wallet lacks reserved funds; resolve held transfers and retry',
                held_transfer.id;
        END IF;

        UPDATE accounts
        SET balance_paise = balance_paise + held_transfer.amount_paise
        WHERE id = hold_account_id;
        INSERT INTO ledger_entries(transfer_id, account_id, direction, amount_paise)
        VALUES
            (held_transfer.id, held_transfer.sender_account_id, 'debit', held_transfer.amount_paise),
            (held_transfer.id, hold_account_id, 'credit', held_transfer.amount_paise);
    END LOOP;
END;
$$;

INSERT INTO schema_migrations(version)
VALUES ('002_fee_and_hold_accounts')
ON CONFLICT (version) DO NOTHING;

COMMIT;
