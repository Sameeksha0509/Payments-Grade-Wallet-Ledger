# Payments-Grade Wallet Ledger

A small FastAPI and PostgreSQL wallet backend. Money is stored as integer paise; every transfer, including a held transfer, has balanced, append-only debit/credit entries. The API uses explicit SQL and transactions so the money path stays visible.

## Run it

Install Docker Desktop, then run this single command from the project directory:

```powershell
docker compose up
```

The API is available at <http://localhost:8000>; interactive API docs are at <http://localhost:8000/docs>. PostgreSQL 16 is initialized from `migrations/001_init.sql` and `migrations/002_fee_and_hold_accounts.sql`. The migration service also applies migration 002 to an existing Compose database volume before the API and worker start. A separate worker schedules reconciliation and risk-flag explanations in UTC. To stop the services, press Ctrl+C. Avoid `docker compose down -v` unless you intentionally want to erase the local database.

For local development, install `requirements.txt`, apply both SQL migrations in order to PostgreSQL 15+, and run `uvicorn app.main:app --reload`. Set `DATABASE_URL` as appropriate. `ADMIN_TOKEN` defaults to `local-admin-token` for local use only; change it outside a local demo.

## Agreed wallet assumptions

- Peer transfers charge a fixed fee of **100 paise by default**, on top of the requested transfer amount. The sender must have enough for both. Set `TRANSFER_FEE_PAISE` to another positive whole-paise amount to configure it; zero and non-integer values are rejected.
- A peer transfer and its fee are separate, linked transfers created atomically. The fee transfer credits the system fee-revenue account. Reversing a peer transfer also creates a linked reversal to refund its fee. The API response includes `fee_paise` and `fee_transfer_id`.
- A held transfer immediately reserves its amount from the sender wallet into the system hold account. This means the displayed wallet balance decreases while the transfer is held. Releasing moves the reserved amount to its intended recipient; rejecting returns it to the sender. A peer transfer's linked fee is reserved and settled with it.
- Migration 002 backfills existing unreserved held transfers by reserving their funds. If an old held transfer's sender wallet no longer has the required funds, startup migration fails rather than silently creating a negative wallet; resolve or reject that hold with the previous application before retrying `docker compose up`.
- Direct transfers have two ledger entries. A held transfer has two reservation entries while held, then four total entries after release or rejection. In every case, each transfer's debit and credit totals balance.
- Reconciliation records every detected finding independently, including transfer imbalance, missing entries, affected stored balances, and global book imbalance. A single underlying corruption may therefore produce multiple report rows.
- The URL `user_id` is a demo identity boundary, not production authentication. Use authenticated identity and authorization before production deployment.

## First API walkthrough

Create a user, then create two wallets. Every POST needs a UUID `Idempotency-Key`:

```powershell
$key = [guid]::NewGuid().ToString()
$user = Invoke-RestMethod -Method Post http://localhost:8000/users `
  -Headers @{"Idempotency-Key"=$key} `
  -ContentType "application/json" `
  -Body '{"name":"Asha Rao","email":"asha@example.test","phone":"+919900000001"}'
$userId = $user.id

$wallet = Invoke-RestMethod -Method Post "http://localhost:8000/users/$userId/wallets" `
  -Headers @{"Idempotency-Key"=[guid]::NewGuid().ToString()} `
  -ContentType "application/json" -Body '{"name":"Main"}'
```

Create another user and wallet to use as a payee. Top up using
`POST /users/{user_id}/top-ups` with `{"wallet_id": 1, "amount_paise": 50000}`.
Transfer using `POST /users/{user_id}/transfers` with
`{"from_account_id": 1, "to_account_id": 2, "amount_paise": 1000, "note": "Lunch"}`.
The request body contains only integer paise. Top-ups, withdrawals, peer transfers,
and reversals all use atomic ledger flows. A peer transfer charges the configured
fee in addition to the requested amount. A transfer can respond with
`201` when completed or `202` when held for review.

Useful endpoints:

| Method | Path | Purpose |
|---|---|---|
| POST | `/users` | Register a user |
| POST | `/users/{user_id}/wallets` | Open an INR wallet |
| GET | `/wallets/{account_id}/balance?user_id=...` | Contract-compatible balance route |
| GET | `/wallets/{account_id}/statement?user_id=...` | Contract-compatible statement route |
| POST | `/users/{user_id}/top-ups` | Top up a wallet |
| POST | `/users/{user_id}/transfers` | Transfer between wallets |
| POST | `/users/{user_id}/withdrawals` | Withdraw to the cash-out system account |
| POST | `/users/{user_id}/transfers/{transfer_id}/reverse` | Create an append-only reversal |
| POST | `/transfers/topup?user_id=...` | Contract-compatible top-up route |
| POST | `/transfers?user_id=...` | Contract-compatible peer-transfer route |
| POST | `/transfers/withdraw?user_id=...` | Contract-compatible withdrawal route |
| POST | `/transfers/{transfer_id}/reverse?user_id=...` | Contract-compatible reversal route |
| GET | `/transfers/{transfer_id}?user_id=...` | Transfer, ledger entries, and status history |
| GET | `/users/{user_id}/wallets/{account_id}/balance` | Read wallet balance |
| GET | `/users/{user_id}/wallets/{account_id}/statement` | Paginated statement; supports `before_id` cursor |
| POST | `/users/{user_id}/statement/ask` | Ledger-grounded statement question |
| POST | `/admin/reconciliation/run` | Run reconciliation (202; key required) |
| GET | `/admin/reconciliation/runs` | List reconciliation reports |
| GET | `/admin/reconciliation/runs/{run_id}` | Fetch a report and its problems |
| GET | `/admin/risk/queue` | Held and flagged transfers |
| POST | `/admin/risk/{transfer_id}/decision` | Release or cancel a held transfer |
| POST | `/admin/transfers/{transfer_id}/release` | Release a held transfer |

Admin endpoints require `Authorization: Bearer <ADMIN_TOKEN>` and an
`Idempotency-Key`. All API errors use `{ "code", "message", "request_id" }`.

## Core design and trade-offs

- **Balances:** store current balances for fast reads, and verify them against the append-only ledger every night. Every write locks affected account rows in ascending ID order, verifies available wallet funds while locked, inserts the ledger rows, and updates the stored balances within one PostgreSQL `READ COMMITTED` transaction. The check constraint disallows negative wallet balances as a final safeguard. System clearing accounts may go negative.
- **Unified accounts:** a user wallet and each system clearing account share one table, so ledger lines always point to exactly one account. `user_id` is nullable only for system accounts; a check constraint separates account kinds.
- **Money:** amounts and balances use `BIGINT` paise; direction is a separate `debit`/`credit` value. This avoids floating-point rounding. Every transfer's deferred database trigger checks for at least two entries and equal debit/credit totals at commit.
- **Immutability and corrections:** a database trigger rejects ledger updates/deletes. A reversal is a new transfer with the opposite account movement and a reference to the original.
- **Concurrency:** `READ COMMITTED` with explicit `SELECT ... FOR UPDATE` locks is sufficient for wallet spending because the relevant balances are re-read after locking. A fixed ascending account ID order avoids transfer-to-transfer lock inversion. `SERIALIZABLE` would increase retry/conflict work without removing the need to handle retries.
- **Idempotency:** a unique `(user_id, idempotency_key)` constraint and a canonical SHA-256 request hash ensure retries return the saved HTTP status/body and a mismatched body returns `422`. User registration uses a null-user key namespace with a separate unique index.
- **Fees and holds:** peer movements create a principal transfer and a linked fee transfer within one database transaction. A hold reserves the principal and fee in the system hold account immediately; admin release or rejection settles both atomically. A peer reversal refunds both principal and fee.
- **Fraud:** thresholds and actions live in `risk_rules`. Velocity and amount-spike matches hold transfers by reserving funds in the hold account. A first large payment to a new payee is allowed with a flag. Admin releases or rejects a hold explicitly.
- **Questions and LLM:** the default mock needs no API key. Set `LLM_PROVIDER=openai` and `OPENAI_API_KEY` to use the OpenAI-compatible parser/explainer. The model produces a constrained JSON intent only; Python validates it and runs parameterized queries scoped to the requested user's wallets. It never generates SQL. The worker treats notes as untrusted input and returns a limited explanation/label.
- **Identity assumption:** this learning project uses the URL's `user_id` as the caller identity for user endpoints. A production deployment must replace that demo boundary with authenticated identity and authorization.
- **Indexes:** account statement and outgoing-fraud queries have targeted indexes. Indexes speed reads at a write/storage cost. Offset pagination is retained for the required contract; cursor pagination is also supported by `(created_at, id)` via `before_id`.
- **Scheduled jobs:** the worker runs reconciliation at 00:00 UTC and risk explanations at 00:15 UTC. Manual reconciliation and the worker use a `NOWAIT` row lock, and the reconciliation checks run in one `REPEATABLE READ` transaction. All detected findings are retained, including overlapping symptoms of one corruption.

## Schema, labs, and tests

- [docs/schema.md](docs/schema.md): noun inventory, cardinalities, and ER diagram.
- [docs/isolation.md](docs/isolation.md): two-session isolation lab and expected observations.
- [docs/operations.md](docs/operations.md): reconciliation SQL, indexes, and query-plan guidance.
- [docs/statement-questions.md](docs/statement-questions.md): ten sample questions and expected results.
- [docs/demo-script.md](docs/demo-script.md): command-by-command five-minute demo recording runbook.
- `python scripts/seed.py`: create 1,000 seed users, 2,000 wallets, and 100,000 completed balanced top-ups in a clean migrated database. Use `python scripts/seed.py --transfers 500000` to generate one million ledger entries for query-plan experiments.
- `pytest -q`: unit tests and PostgreSQL integration tests (the latter are skipped unless `TEST_DATABASE_URL` points to a dedicated migrated test database). For a full local run, create a disposable database, apply migrations 001 and 002, and use the Compose API image as shown below:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test.ps1
```

This single test command starts PostgreSQL if needed, creates a unique
disposable test database, applies both migrations, runs every test in the
Compose Python image, and drops only the disposable database afterward. It
does not erase the normal `wallet` database or its volume.

The integration suite tests 10 parallel replays of one idempotency key (plus
sequential replay and mismatched-body rejection), 10 statement questions
including a prompt-injection attempt, independent reconciliation corruptions,
held-transfer release/rejection, fees, reversals, and the 100-request/20-round
spending stampede. It temporarily changes risk rules for relevant scenarios and
restores them afterwards.

## Next steps for production

Add real authentication/authorization, operational secrets management, rate limits,
key expiration, richer reversal policies, provider timeouts/retries and observability,
and a production-grade LLM evaluation set. The demo runbook documents the recording
steps; the video itself must be recorded and submitted separately.
<img width="574" height="337" alt="09-statement-qa" src="https://github.com/user-attachments/assets/95efd0c8-5d9d-4f6b-b2eb-8855fd2389c6" />
<img width="488" height="177" alt="08-risk-hold-audit-history" src="https://github.com/user-attachments/assets/7823f533-aed5-47de-a773-534fcc8817ff" />
<img width="353" height="434" alt="07-idempotency-retry" src="https://github.com/user-attachments/assets/58aee72a-eee8-40e6-a3ec-aedab1e7a063" />
<img width="355" height="435" alt="06-transfer-demo" src="https://github.com/user-attachments/assets/d0d224c8-1525-413e-93c9-cea65200ba85" />
<img width="539" height="253" alt="05-reconciliation" src="https://github.com/user-attachments/assets/ea2cc86f-acb2-427e-97e1-89c380565c35" />
<img width="656" height="103" alt="04-ledger-integrity" src="https://github.com/user-attachments/assets/ee04c0e1-bfd0-4f18-8069-cd22d0356815" />
<img width="654" height="87" alt="03-seed-transfers" src="https://github.com/user-attachments/assets/42b64f4d-3486-46b6-809f-22a31a779d57" />
<img width="653" height="210" alt="02-integration-tests" src="https://github.com/user-attachments/assets/cdaa3390-73f3-4ff3-abf2-5ef5b4f9ba3a" />
<img width="665" height="264" alt="01-api-health" src="https://github.com/user-attachments/assets/2e3132ee-be3e-4922-bf51-42bf84c89d22" />
