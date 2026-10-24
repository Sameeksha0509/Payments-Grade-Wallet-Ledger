# Five-minute demo recording runbook

This is a command-by-command recording guide, not a video file. Record the
Swagger UI and a PowerShell terminal in one screen capture. Start with a clean
demo database so the velocity rule behavior and reconciliation result are
predictable. Do not run `docker compose down -v` against a database you need.

## 1. Start the project

From the repository root, run this in PowerShell:

```powershell
docker compose up --build -d
docker compose ps
Invoke-RestMethod http://localhost:8000/health
Start-Process http://localhost:8000/docs
```

Show that `db`, `api`, and `worker` are running, `/health` says `ok`, and
Swagger is available. For an already-used database, the demo below creates
fresh users and uses unique idempotency keys.

## 2. Set up two users and wallets

Run this in the same PowerShell session. The API's local default admin token is
used unless `ADMIN_TOKEN` is set in the Compose environment:

```powershell
$adminToken = (docker compose exec -T api printenv ADMIN_TOKEN).Trim()
$userSuffix = [guid]::NewGuid().ToString("N")

$asha = Invoke-RestMethod -Method Post http://localhost:8000/users `
  -Headers @{"Idempotency-Key" = [guid]::NewGuid().ToString()} `
  -ContentType "application/json" `
  -Body (@{
    name = "Asha Demo"
    email = "asha-$userSuffix@example.test"
    phone = "+1555$($userSuffix.Substring(0,10))"
  } | ConvertTo-Json)

$rahul = Invoke-RestMethod -Method Post http://localhost:8000/users `
  -Headers @{"Idempotency-Key" = [guid]::NewGuid().ToString()} `
  -ContentType "application/json" `
  -Body (@{
    name = "Rahul Demo"
    email = "rahul-$userSuffix@example.test"
    phone = "+1666$($userSuffix.Substring(0,10))"
  } | ConvertTo-Json)

$ashaWallet = Invoke-RestMethod -Method Post `
  "http://localhost:8000/users/$($asha.id)/wallets" `
  -Headers @{"Idempotency-Key" = [guid]::NewGuid().ToString()} `
  -ContentType "application/json" -Body '{"name":"Main"}'

$rahulWallet = Invoke-RestMethod -Method Post `
  "http://localhost:8000/users/$($rahul.id)/wallets" `
  -Headers @{"Idempotency-Key" = [guid]::NewGuid().ToString()} `
  -ContentType "application/json" -Body '{"name":"Main"}'

$ashaId = $asha.id
$rahulId = $rahul.id
$ashaAccount = $ashaWallet.id
$rahulAccount = $rahulWallet.id
```

## 3. Top up, transfer, and retry

Top up Asha with 50,000 paise. Send 1,000 paise to Rahul; the default 100-paise
fee is charged on top. Then replay the exact request with the same key and show
that the original response and transfer ID are returned:

```powershell
$topUp = Invoke-RestMethod -Method Post `
  "http://localhost:8000/users/$ashaId/top-ups" `
  -Headers @{"Idempotency-Key" = [guid]::NewGuid().ToString()} `
  -ContentType "application/json" `
  -Body (@{wallet_id = $ashaAccount; amount_paise = 50000} | ConvertTo-Json)

$firstTransferKey = [guid]::NewGuid().ToString()
$firstTransferBody = @{
  from_account_id = $ashaAccount
  to_account_id = $rahulAccount
  amount_paise = 1000
  note = "Demo lunch"
} | ConvertTo-Json

$firstTransfer = Invoke-RestMethod -Method Post `
  "http://localhost:8000/users/$ashaId/transfers" `
  -Headers @{"Idempotency-Key" = $firstTransferKey} `
  -ContentType "application/json" -Body $firstTransferBody

$firstTransferReplay = Invoke-RestMethod -Method Post `
  "http://localhost:8000/users/$ashaId/transfers" `
  -Headers @{"Idempotency-Key" = $firstTransferKey} `
  -ContentType "application/json" -Body $firstTransferBody

$firstTransfer
$firstTransferReplay
Invoke-RestMethod "http://localhost:8000/users/$ashaId/wallets/$ashaAccount/balance"
Invoke-RestMethod "http://localhost:8000/users/$rahulId/wallets/$rahulAccount/balance"
Invoke-RestMethod "http://localhost:8000/transfers/$($firstTransfer.transfer_id)?user_id=$ashaId"
```

Show that both calls have the same `transfer_id`, the peer transfer and fee are
separate ledger transfers, and the balances reflect the transfer plus fee.

## 4. Hold and release a transfer

The first completed transfer satisfies the velocity rule once its threshold is
set to one. Lower the threshold, send another transfer, and show that the
principal and fee are reserved in the hold account while the status is `held`:

```powershell
docker compose exec -T db psql -U wallet -d wallet -v ON_ERROR_STOP=1 `
  -c "UPDATE risk_rules SET threshold = 1, window_minutes = 10, action = 'hold', enabled = TRUE WHERE rule_type = 'velocity'"

$heldTransfer = Invoke-RestMethod -Method Post `
  "http://localhost:8000/users/$ashaId/transfers" `
  -Headers @{"Idempotency-Key" = [guid]::NewGuid().ToString()} `
  -ContentType "application/json" `
  -Body (@{
    from_account_id = $ashaAccount
    to_account_id = $rahulAccount
    amount_paise = 1000
    note = "Demo held transfer"
  } | ConvertTo-Json)

$heldTransfer
Invoke-RestMethod "http://localhost:8000/users/$ashaId/wallets/$ashaAccount/balance"
Invoke-RestMethod http://localhost:8000/admin/risk/queue `
  -Headers @{Authorization = $adminToken}
```

Release it and show the status transition and audit trail:

```powershell
$release = Invoke-RestMethod -Method Post `
  "http://localhost:8000/admin/risk/$($heldTransfer.transfer_id)/decision" `
  -Headers @{
    Authorization = $adminToken
    "Idempotency-Key" = [guid]::NewGuid().ToString()
  } `
  -ContentType "application/json" -Body '{"action":"release"}'

$release
Invoke-RestMethod "http://localhost:8000/transfers/$($heldTransfer.transfer_id)?user_id=$ashaId"
```

The hold account should now have returned to its prior balance. Restore the
default velocity rule for future demos:

```powershell
docker compose exec -T db psql -U wallet -d wallet -v ON_ERROR_STOP=1 `
  -c "UPDATE risk_rules SET threshold = 5, window_minutes = 10, action = 'hold', enabled = TRUE WHERE rule_type = 'velocity'"
```

## 5. Run reconciliation and ask a statement question

Run reconciliation with a unique idempotency key, then fetch its persisted
report. On an uncorrupted demo database, it should pass with zero problems:

```powershell
$reconciliation = Invoke-RestMethod -Method Post `
  http://localhost:8000/admin/reconciliation/run `
  -Headers @{
    Authorization = $adminToken
    "Idempotency-Key" = [guid]::NewGuid().ToString()
  }

$reconciliation
Invoke-RestMethod `
  "http://localhost:8000/admin/reconciliation/runs/$($reconciliation.run_id)" `
  -Headers @{Authorization = $adminToken}
```

Ask about the completed payments over today's date range. The response is
calculated from Asha's ledger entries and should include the two peer-transfer
IDs and a total of 2,000 paise, excluding separately represented fees:

```powershell
$today = Get-Date -Format "yyyy-MM-dd"
$question = "How much did I send to Rahul Demo from $today to $today?"

Invoke-RestMethod -Method Post `
  "http://localhost:8000/users/$ashaId/statement/ask" `
  -Headers @{"Idempotency-Key" = [guid]::NewGuid().ToString()} `
  -ContentType "application/json" `
  -Body (@{question = $question} | ConvertTo-Json)
```

## Recording checklist

- Keep the Swagger page and PowerShell output visible in the recording.
- Show one completed transfer, an exact-key retry, the held response and
  reserved balance, the admin release, a passing reconciliation report, and
  the ledger-grounded Q&A answer.
- Narrate that money and fees are integer paise, the sender pays the fee on
  top, and held funds are reserved in a system account.
- Reset the velocity rule as shown. `docker compose down` stops services but
  preserves the database. Do not use `docker compose down -v` unless you want
  to delete the local database volume.
