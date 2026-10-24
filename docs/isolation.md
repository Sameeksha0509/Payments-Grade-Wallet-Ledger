# PostgreSQL isolation and locking lab

The money-writing service uses `READ COMMITTED` plus explicit row locks. The
commands below are a reproducible lab against a migrated local database. Open
two terminals and run `docker compose exec db psql -U wallet -d wallet` in each.
Use three existing wallets with enough balance: one for the read-committed and
repeatable-read exercises, and two different wallets for the serializable
exercise. These exercises directly change cached balances without ledger
entries, so record the original balances and restore them at the end before
using reconciliation.

## Setup

Session A:

```sql
BEGIN;
SELECT id, balance_paise FROM accounts
WHERE account_type = 'wallet' ORDER BY id LIMIT 1;
-- Record this wallet's original balance, then enter both values:
\set wallet_id 1
\set original_balance 10000
```

Session B (use the ID from A):

```sql
BEGIN;
UPDATE accounts SET balance_paise = balance_paise + 100
WHERE id = :wallet_id;
COMMIT;
```

## Read Committed

In Session A:

```sql
SHOW transaction_isolation;
SELECT balance_paise FROM accounts WHERE id = :wallet_id;
COMMIT;
```

Expected: `read committed`; Session A's second SELECT sees Session B's committed
increase. Each statement takes a fresh snapshot.

Illustrative output (using an account whose balance starts at `10000`):

```text
-- Session A, first SELECT
 balance_paise
---------------
         10000

-- Session B commits an increase of 100 paise
-- Session A, second SELECT
 balance_paise
---------------
         10100
```

## Repeatable Read and a join

Session A:

```sql
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT balance_paise FROM accounts WHERE id = :wallet_id;
SELECT a.balance_paise, u.name
FROM accounts a JOIN users u ON u.id = a.user_id
WHERE a.id = :wallet_id;
```

While A is open, Session B repeats and commits the +100 UPDATE. Run both SELECT
statements again in A, then `COMMIT`.

Expected: both the direct read and the joined read keep seeing A's original
snapshot. A repeatable-read snapshot applies across joined tables too; it is not
limited to a single-table query.

Illustrative output:

```text
-- Session A, first direct SELECT and joined SELECT: 10000
-- Session B commits an increase of 100 paise
-- Session A, repeated direct SELECT and joined SELECT: 10000
```

## Serializable write skew

Choose two wallet rows with at least 100 paise each. In both sessions, enter
their IDs as psql variables, for example `\set wallet_a 1` and
`\set wallet_b 2`, and record their original balances as
`\set original_wallet_a 10000` and `\set original_wallet_b 20000`. In Session A:

```sql
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT SUM(balance_paise) FROM accounts WHERE id IN (:wallet_a, :wallet_b);
UPDATE accounts SET balance_paise = balance_paise - 100 WHERE id = :wallet_a;
-- Leave Session A open; do not commit yet.
```

In Session B, run the same sequence, updating `:wallet_b` instead of
`:wallet_a`, and commit Session B. Then commit Session A. Both sessions base
their change on the shared balance predicate.

Expected: PostgreSQL aborts one transaction with SQLSTATE `40001` (serialization
failure), because the concurrent aggregate reads and separate writes form a
serialization cycle. The application must retry a serialization failure if it
chooses SERIALIZABLE.

Illustrative output for the losing session:

```text
ERROR:  could not serialize access due to read/write dependencies among transactions
SQLSTATE: 40001
```

In this sequence Session B commits first; Session A gets the serialization
failure when it tries to commit.

These are expected observations and illustrative output; run the lab against
your migrated local PostgreSQL instance to observe the concurrent sessions.

## Step 2: demonstrate the unlocked lost update

Use a disposable scratch table (not a wallet account, and not the service's
ledger). Run once:

```sql
CREATE TABLE unsafe_lab_wallet (id INTEGER PRIMARY KEY, balance_paise BIGINT NOT NULL);
INSERT INTO unsafe_lab_wallet VALUES (1, 100);
```

In both sessions, start the transaction and read the same balance before either
session updates it:

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT balance_paise FROM unsafe_lab_wallet WHERE id = 1;
-- Application code computes 100 - 80 = 20 from this unlocked read.
```

Session A then runs `UPDATE unsafe_lab_wallet SET balance_paise = 20 WHERE id =
1; COMMIT;`. Session B runs that same update using its stale read and commits.
The final balance is `20`, even though both callers each attempted to spend 80
paise. This is a lost update; the stored balance falsely suggests only one
spend. The scratch table deliberately omits ledger entries and must never be
used by the application. Remove it with
`DROP TABLE unsafe_lab_wallet;` after the exercise.

## Restore the wallet balances

After all exercises, run this once in either session, substituting the values
recorded before starting. This leaves the ledger-backed balances unchanged:

```sql
UPDATE accounts SET balance_paise = :original_balance WHERE id = :wallet_id;
UPDATE accounts SET balance_paise = :original_wallet_a WHERE id = :wallet_a;
UPDATE accounts SET balance_paise = :original_wallet_b WHERE id = :wallet_b;
```

## Why the transfer uses READ COMMITTED

For a transfer, the application selects all involved accounts in ascending
`account_id` order `FOR UPDATE`, then checks the source wallet's current balance,
writes both ledger lines, updates cached balances, and commits. A competing
transfer cannot pass the balance check until it acquires the same wallet row
lock; its subsequent read sees the preceding commit. Lock ordering prevents
opposite-direction transfers from taking account locks in opposite orders.
This protects the spend invariant without broad serializable conflict retries.

The application rejects insufficient funds before writing any transfer/ledger
rows, and the wallet balance check constraint is a final database-level guard.
The deferred transfer trigger ensures a completed transfer has at least two
balanced entries when its transaction commits.
