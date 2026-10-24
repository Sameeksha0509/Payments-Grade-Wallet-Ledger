# Operational queries and performance notes

## Reconciliation checks

`app/reconciliation.py` runs the checks in one `REPEATABLE READ` transaction,
holding the `nightly_reconciliation` row in `job_locks` with `FOR UPDATE NOWAIT`.
It records mismatches without modifying balances or ledger history.

The report records every observed issue independently. A transfer imbalance may
therefore appear alongside affected stored-balance differences and a global
debit/credit mismatch; these are useful separate findings, not deduplicated
symptoms. Held transfers have a debit/credit reservation pair in the hold
account; after settlement they have two balanced pairs. Fee transfers are
checked like any other transfer.

Equivalent core checks:

```sql
-- All books total (per-transfer checks identify the underlying defects).
SELECT
  COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'debit'), 0) AS debits,
  COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'credit'), 0) AS credits
FROM ledger_entries;

-- Every committed transfer has at least two lines and balanced totals.
-- A settled hold has two balanced pairs, represented by hold-account lines.
SELECT t.id
FROM transfers t
LEFT JOIN ledger_entries l ON l.transfer_id = t.id
LEFT JOIN accounts a ON a.id = l.account_id
GROUP BY t.id, t.amount_paise, t.status
HAVING COUNT(l.id) < 2
    OR COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'debit'), 0)
       <> COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'credit'), 0)
    OR COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'debit'), 0)
       <> CASE WHEN COUNT(l.id) FILTER (WHERE a.account_type = 'hold') = 2
               THEN t.amount_paise * 2 ELSE t.amount_paise END;

-- Stored balance versus ledger-derived balance.
SELECT a.id, a.balance_paise,
       COALESCE(SUM(CASE WHEN l.direction = 'credit'
                         THEN l.amount_paise ELSE -l.amount_paise END), 0) AS computed
FROM accounts a LEFT JOIN ledger_entries l ON l.account_id = a.id
GROUP BY a.id
HAVING a.balance_paise <> COALESCE(
  SUM(CASE WHEN l.direction = 'credit' THEN l.amount_paise ELSE -l.amount_paise END), 0
);

-- Wallet overdrafts, computed from entries.
SELECT a.id
FROM accounts a JOIN ledger_entries l ON l.account_id = a.id
WHERE a.account_type = 'wallet'
GROUP BY a.id
HAVING SUM(CASE WHEN l.direction = 'credit' THEN l.amount_paise ELSE -l.amount_paise END) < 0;

-- Any transfer with fewer than two entries and orphan ledger entries.
SELECT t.id
FROM transfers t LEFT JOIN ledger_entries l ON l.transfer_id = t.id
GROUP BY t.id HAVING COUNT(l.id) < 2;
SELECT l.id FROM ledger_entries l
LEFT JOIN transfers t ON t.id = l.transfer_id WHERE t.id IS NULL;
```

The foreign keys make orphan rows impossible under normal constraints, but the
query remains as an explicit audit. Correlated findings are intentionally not
suppressed: transfer-level mismatches, affected cached balances, and a global
book mismatch are each recorded when observed.

## Index rationale and query plans

- `ledger_account_statement_idx(account_id, created_at DESC, id DESC)` supports
  a wallet statement and its cursor.
- `ledger_transfer_idx(transfer_id)` supports transfer validation and
  reconciliation joins.
- `transfers_sender_recent_idx(sender_account_id, status, created_at DESC)`
  supports velocity and 30-day amount-spike checks.
- `transfers_sender_recipient_idx(sender_account_id, recipient_account_id,
  status)` supports the new-payee check.

Run against a fresh database after seeding 500,000 transfers (one million
ledger entries) with `python scripts/seed.py --transfers 500000`:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, direction, amount_paise, created_at
FROM ledger_entries
WHERE account_id = 1
ORDER BY created_at DESC, id DESC
LIMIT 50 OFFSET 100000;

EXPLAIN (ANALYZE, BUFFERS)
SELECT id, direction, amount_paise, created_at
FROM ledger_entries
WHERE account_id = 1
  AND (created_at, id) < (TIMESTAMPTZ '2026-01-01 00:00:00+00', 100000)
ORDER BY created_at DESC, id DESC
LIMIT 50;
```

No measured timings are claimed here because this environment had no running
Docker/PostgreSQL daemon. Offset pagination must walk and discard the preceding 100,000
matching index entries; the cursor plan can seek to a composite index boundary.
The nightly integrity scans can afford full scans because they run once daily;
the transfer path cannot, so its recent-activity queries need the sender index.
The reconciliation joins require the transfer and account ledger indexes.

Measure this compound index's write overhead under the actual workload before
adding further indexes.

For a local latency baseline, run each rule query through `EXPLAIN (ANALYZE,
BUFFERS)` before/after a transfer workload. These representative parameterized
queries exercise the specific indexes above; retain the reported execution
times with the environment and row counts when measuring:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*) FROM transfers
WHERE sender_account_id = 42 AND status IN ('completed', 'held')
  AND created_at >= now() - interval '10 minutes';

EXPLAIN (ANALYZE, BUFFERS)
SELECT AVG(amount_paise) FROM transfers
WHERE sender_account_id = 42 AND status = 'completed'
  AND created_at >= now() - interval '30 days';

EXPLAIN (ANALYZE, BUFFERS)
SELECT 1 FROM transfers
WHERE sender_account_id = 42 AND recipient_account_id = 84
  AND status = 'completed'
LIMIT 1;
```

The transfer-path rule checks should use the sender-recent and
sender-recipient indexes; those are paid on every transfer write. No timing
values are claimed here because a PostgreSQL instance was not available in the
implementation environment.
