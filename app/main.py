import hashlib
import json
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

import psycopg
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError

from app.reconciliation import run_reconciliation
from app.statement_llm import get_statement_parser


app = FastAPI(title="Payments-Grade Wallet Ledger", version="1.0.0")
logging.basicConfig(level=logging.INFO)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
        },
    )


def idempotent_response(status_code: int, body: dict[str, Any]):
    stable_body = json.loads(json.dumps(body, sort_keys=True, separators=(",", ":")))
    return JSONResponse(status_code=status_code, content=stable_body)


@app.exception_handler(psycopg.Error)
async def database_error_handler(request: Request, exc: psycopg.Error):
    logging.exception("Database request failed", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "code": "DATABASE_ERROR",
            "message": "The request could not be completed.",
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
        },
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logging.exception("Unexpected request failure", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL_ERROR",
            "message": "The request could not be completed.",
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
        },
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    missing_key = any(
        error.get("loc") == ("header", "Idempotency-Key")
        for error in exc.errors()
    )
    return JSONResponse(
        status_code=400 if missing_key else 422,
        content={
            "code": "IDEMPOTENCY_KEY_REQUIRED" if missing_key else "INVALID_REQUEST",
            "message": (
                "Idempotency-Key header is required."
                if missing_key
                else "The request body or parameters are invalid."
            ),
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": "HTTP_ERROR",
            "message": str(exc.detail),
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
        },
    )


@contextmanager
def connection():
    with psycopg.connect(
        os.getenv("DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet"),
        row_factory=dict_row,
    ) as conn:
        yield conn


def require_key(value: str | None) -> uuid.UUID:
    if value is None:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required.")
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be a UUID.") from exc


def canonical_hash(body: dict[str, Any]) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def replay_or_start(conn, user_id: int | None, key: uuid.UUID, body: dict[str, Any]):
    digest = canonical_hash(body)
    row = conn.execute(
        """
        INSERT INTO idempotency_keys(user_id, idempotency_key, request_hash, status)
        VALUES (%s, %s, %s, 'processing')
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (user_id, key, digest),
    ).fetchone()
    if row:
        return row["id"], None
    existing = conn.execute(
        """
        SELECT request_hash, status, response_code, response_body
        FROM idempotency_keys
        WHERE user_id IS NOT DISTINCT FROM %s AND idempotency_key = %s
        """,
        (user_id, key),
    ).fetchone()
    if not existing:
        raise ApiError(409, "IDEMPOTENCY_RETRY", "Retry the request shortly.")
    if existing["request_hash"].strip() != digest:
        raise ApiError(422, "IDEMPOTENCY_KEY_REUSED", "This key was already used with a different request body.")
    if existing["status"] != "completed":
        raise ApiError(409, "IDEMPOTENCY_IN_PROGRESS", "Retry the request shortly.")
    return None, (existing["response_code"], existing["response_body"])


def save_idempotent_response(conn, key_id: int, status_code: int, body: dict[str, Any]):
    conn.execute(
        """
        UPDATE idempotency_keys
        SET status = 'completed', response_code = %s, response_body = %s
        WHERE id = %s
        """,
        (status_code, Jsonb(body), key_id),
    )


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    phone: str = Field(min_length=3, max_length=32)


class WalletCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)


class Movement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_account_id: int
    to_account_id: int
    amount_paise: int = Field(strict=True, gt=0, le=9223372036854775807)
    note: str = Field(default="", max_length=1000)


class WalletMovement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wallet_id: int
    amount_paise: int = Field(strict=True, gt=0, le=9223372036854775807)
    note: str = Field(default="", max_length=1000)


class ReverseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=1000)


class TransferDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(pattern="^(release|reverse)$")


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


def _user_exists(conn, user_id: int):
    if not conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone():
        raise ApiError(404, "USER_NOT_FOUND", "User was not found.")


def create_user(body: UserCreate, key: uuid.UUID):
    payload = body.model_dump()
    with connection() as conn, conn.transaction():
        key_id, replay = replay_or_start(conn, None, key, payload)
        if replay:
            return replay
        try:
            user = conn.execute(
                """
                INSERT INTO users(name, email, phone)
                VALUES (%s, %s, %s)
                RETURNING id, name, email, phone, kyc_status, created_at
                """,
                (body.name, body.email.lower(), body.phone),
            ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise ApiError(409, "USER_ALREADY_EXISTS", "Email or phone is already registered.") from exc
        response = dict(user)
        response["created_at"] = response["created_at"].isoformat()
        save_idempotent_response(conn, key_id, 201, response)
        return 201, response


@app.post("/users", status_code=201)
def register_user(body: UserCreate, idempotency_key: str = Header(..., alias="Idempotency-Key")):
    key = require_key(idempotency_key)
    result = create_user(body, key)
    return idempotent_response(result[0], result[1])


@app.post("/users/{user_id}/wallets", status_code=201)
def open_wallet(
    user_id: int,
    body: WalletCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    key = require_key(idempotency_key)
    payload = body.model_dump()
    with connection() as conn, conn.transaction():
        key_id, replay = replay_or_start(conn, user_id, key, payload)
        if replay:
            return idempotent_response(replay[0], replay[1])
        _user_exists(conn, user_id)
        account = conn.execute(
            """
            INSERT INTO accounts(user_id, name, account_type)
            VALUES (%s, %s, 'wallet')
            RETURNING id, user_id, name, account_type, currency, balance_paise
            """,
            (user_id, body.name),
        ).fetchone()
        response = dict(account)
        save_idempotent_response(conn, key_id, 201, response)
        return idempotent_response(201, response)


def _risk_decisions(conn, sender_id: int, recipient_id: int, amount: int, transfer_type: str):
    rules = conn.execute("SELECT * FROM risk_rules WHERE enabled = TRUE ORDER BY id").fetchall()
    decisions: list[tuple[dict[str, Any], bool]] = []
    for rule in rules:
        fires = False
        if rule["rule_type"] == "velocity":
            count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM transfers
                WHERE sender_account_id = %s AND transfer_type <> 'fee'
                  AND status IN ('completed', 'held')
                  AND created_at >= now() - (%s * interval '1 minute')
                """,
                (sender_id, rule["window_minutes"]),
            ).fetchone()["count"]
            fires = count >= rule["threshold"]
        elif rule["rule_type"] == "amount_spike":
            average = conn.execute(
                """
                SELECT AVG(amount_paise) AS average
                FROM transfers
                WHERE sender_account_id = %s AND transfer_type <> 'fee'
                  AND status = 'completed'
                  AND created_at >= now() - interval '30 days'
                """,
                (sender_id,),
            ).fetchone()["average"]
            fires = bool(average and amount > rule["threshold"] * average)
        elif rule["rule_type"] == "new_payee" and transfer_type == "peer" and amount > rule["threshold"]:
            seen = conn.execute(
                """
                SELECT 1 FROM transfers
                WHERE sender_account_id = %s AND recipient_account_id = %s AND status = 'completed'
                LIMIT 1
                """,
                (sender_id, recipient_id),
            ).fetchone()
            fires = seen is None
        if fires:
            decisions.append((rule, rule["action"] == "hold"))
    return decisions


def _account_rows_locked(conn, account_ids: list[int]):
    rows = conn.execute(
        """
        SELECT id, user_id, account_type, balance_paise
        FROM accounts
        WHERE id = ANY(%s)
        ORDER BY id
        FOR UPDATE
        """,
        (account_ids,),
    ).fetchall()
    if len(rows) != len(set(account_ids)):
        raise ApiError(404, "ACCOUNT_NOT_FOUND", "One or more accounts were not found.")
    return {row["id"]: row for row in rows}



def _post_ledger(conn, transfer_id: int, sender_id: int, recipient_id: int, amount: int):
    # Debit the sender only if the wallet has sufficient funds.
    # All changes remain inside the caller's database transaction.
    sender_update = conn.execute(
        """
        UPDATE accounts
        SET balance_paise = balance_paise - %s
        WHERE id = %s
          AND (
              account_type <> 'wallet'
              OR balance_paise >= %s
          )
        RETURNING id
        """,
        (amount, sender_id, amount),
    ).fetchone()

    if sender_update is None:
        raise ApiError(
            409,
            "INSUFFICIENT_FUNDS",
            "The wallet does not have enough funds.",
        )

    # Credit the recipient.
    recipient_update = conn.execute(
        """
        UPDATE accounts
        SET balance_paise = balance_paise + %s
        WHERE id = %s
        RETURNING id
        """,
        (amount, recipient_id),
    ).fetchone()

    if recipient_update is None:
        raise ApiError(
            404,
            "ACCOUNT_NOT_FOUND",
            "The recipient account was not found.",
        )

    # Record both sides of the double-entry ledger.
    conn.execute(
        """
        INSERT INTO ledger_entries(
            transfer_id, account_id, direction, amount_paise
        )
        VALUES
            (%s, %s, 'debit', %s),
            (%s, %s, 'credit', %s)
        """,
        (
            transfer_id, sender_id, amount,
            transfer_id, recipient_id, amount,
        ),
    )


def _reserve_held_transfer(
    conn, transfer_id: int, sender_id: int, hold_account_id: int, amount: int
):
    debited = conn.execute(
        """
        UPDATE accounts SET balance_paise = balance_paise - %s
        WHERE id = %s
          AND (account_type <> 'wallet' OR balance_paise >= %s)
        RETURNING id
        """,
        (amount, sender_id, amount),
    ).fetchone()
    if debited is None:
        raise ApiError(409, "INSUFFICIENT_FUNDS", "The wallet does not have enough funds.")
    conn.execute(
        "UPDATE accounts SET balance_paise = balance_paise + %s WHERE id = %s",
        (amount, hold_account_id),
    )
    conn.execute(
        """
        INSERT INTO ledger_entries(transfer_id, account_id, direction, amount_paise)
        VALUES (%s, %s, 'debit', %s), (%s, %s, 'credit', %s)
        """,
        (transfer_id, sender_id, amount, transfer_id, hold_account_id, amount),
    )


def _settle_held_transfer(
    conn,
    transfer: dict[str, Any],
    hold_account_id: int,
    action: str,
):
    amount = transfer["amount_paise"]
    debited = conn.execute(
        """
        UPDATE accounts SET balance_paise = balance_paise - %s
        WHERE id = %s AND balance_paise >= %s
        RETURNING id
        """,
        (amount, hold_account_id, amount),
    ).fetchone()
    if debited is None:
        raise ApiError(409, "HOLD_BALANCE_MISMATCH", "The hold account cannot settle this transfer.")

    destination_id = (
        transfer["recipient_account_id"]
        if action == "release"
        else transfer["sender_account_id"]
    )
    conn.execute(
        "UPDATE accounts SET balance_paise = balance_paise + %s WHERE id = %s",
        (amount, destination_id),
    )
    conn.execute(
        """
        INSERT INTO ledger_entries(transfer_id, account_id, direction, amount_paise)
        VALUES (%s, %s, 'debit', %s), (%s, %s, 'credit', %s)
        """,
        (transfer["id"], hold_account_id, amount, transfer["id"], destination_id, amount),
    )


def _fee_paise() -> int:
    raw_fee = os.getenv("TRANSFER_FEE_PAISE", "100")
    try:
        fee = int(raw_fee)
    except ValueError as exc:
        raise RuntimeError("TRANSFER_FEE_PAISE must be a positive integer.") from exc
    if not 1 <= fee <= 9223372036854775807:
        raise RuntimeError("TRANSFER_FEE_PAISE must be a positive BIGINT value.")
    return fee



def perform_movement(user_id: int, key: uuid.UUID, body: Movement | WalletMovement, transfer_type: str):
    payload = {"type": transfer_type, **body.model_dump()}
    with connection() as conn, conn.transaction():
        key_id, replay = replay_or_start(conn, user_id, key, payload)
        if replay:
            return replay
        _user_exists(conn, user_id)
        conn.execute("SELECT set_config('app.changed_by', %s, true)", (f"user:{user_id}",))
        fee_amount = _fee_paise() if transfer_type == "peer" else 0
        system_accounts = {}
        if transfer_type == "peer":
            system_accounts = {
                row["account_type"]: row["id"]
                for row in conn.execute(
                    """
                    SELECT id, account_type FROM accounts
                    WHERE user_id IS NULL AND account_type IN ('fee_revenue', 'hold')
                    """
                ).fetchall()
            }
            if set(system_accounts) != {"fee_revenue", "hold"}:
                raise ApiError(500, "SYSTEM_ACCOUNT_MISSING", "Required fee or hold account is missing.")
            sender_id, recipient_id = body.from_account_id, body.to_account_id
        else:
            system_type = "cash_in" if transfer_type == "top_up" else "cash_out"
            system_account = conn.execute(
                "SELECT id FROM accounts WHERE user_id IS NULL AND account_type = %s",
                (system_type,),
            ).fetchone()
            if not system_account:
                raise ApiError(500, "SYSTEM_ACCOUNT_MISSING", f"Required {system_type} account is missing.")
            sender_id, recipient_id = (
                (system_account["id"], body.wallet_id)
                if transfer_type == "top_up"
                else (body.wallet_id, system_account["id"])
            )
        if sender_id == recipient_id:
            raise ApiError(422, "INVALID_ACCOUNTS", "Source and destination must be different accounts.")
        account_ids = [sender_id, recipient_id]
        if transfer_type == "peer":
            account_ids.extend(system_accounts.values())
        accounts = _account_rows_locked(conn, account_ids)
        sender = accounts[sender_id]
        recipient = accounts[recipient_id]
        wallet = sender if sender["account_type"] == "wallet" else recipient
        if wallet["user_id"] != user_id:
            raise ApiError(403, "ACCOUNT_ACCESS_DENIED", "The wallet does not belong to this user.")
        if transfer_type == "peer" and (sender["account_type"] != "wallet" or recipient["account_type"] != "wallet"):
            raise ApiError(422, "INVALID_DESTINATION", "Peer transfers must go to a user wallet.")
        if transfer_type == "top_up" and (sender["account_type"] != "cash_in" or recipient["account_type"] != "wallet"):
            raise ApiError(422, "INVALID_DESTINATION", "Top-ups require a user wallet.")
        if transfer_type == "withdrawal" and (sender["account_type"] != "wallet" or recipient["account_type"] != "cash_out"):
            raise ApiError(422, "INVALID_DESTINATION", "Withdrawals require a user wallet.")
        required_balance = body.amount_paise + fee_amount
        if sender["account_type"] == "wallet" and sender["balance_paise"] < required_balance:
            raise ApiError(409, "INSUFFICIENT_FUNDS", "The wallet does not have enough funds.")

        decisions = _risk_decisions(
            conn, wallet["id"], recipient["id"], body.amount_paise, transfer_type
        )
        fee_decisions = (
            _risk_decisions(
                conn, sender["id"], system_accounts["fee_revenue"], fee_amount, "fee"
            )
            if fee_amount
            else []
        )
        held = any(hold for _, hold in (*decisions, *fee_decisions))
        status = "held" if held else "processing"
        transfer = conn.execute(
            """
            INSERT INTO transfers(
                transfer_type, status, amount_paise, note, sender_account_id, recipient_account_id
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (transfer_type, status, body.amount_paise, body.note, sender["id"], recipient["id"]),
        ).fetchone()
        all_decisions = {
            rule["id"]: (rule, hold)
            for rule, hold in (*decisions, *fee_decisions)
        }
        for rule, _ in all_decisions.values():
            conn.execute(
                "INSERT INTO transfer_risk_flags(transfer_id, rule_id) VALUES (%s, %s)",
                (transfer["id"], rule["id"]),
            )
        if held:
            _reserve_held_transfer(
                conn, transfer["id"], sender["id"], system_accounts["hold"], body.amount_paise
            )
        else:
            _post_ledger(conn, transfer["id"], sender["id"], recipient["id"], body.amount_paise)
            conn.execute(
                "UPDATE transfers SET status = 'completed', completed_at = now() WHERE id = %s",
                (transfer["id"],),
            )
            status = "completed"
        fee_transfer = None
        if fee_amount:
            fee_transfer = conn.execute(
                """
                INSERT INTO transfers(
                    transfer_type, status, amount_paise, note, sender_account_id,
                    recipient_account_id, fee_for_transfer_id
                )
                VALUES ('fee', %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    "held" if held else "processing",
                    fee_amount,
                    f"Fee for transfer {transfer['id']}",
                    sender["id"],
                    system_accounts["fee_revenue"],
                    transfer["id"],
                ),
            ).fetchone()
            for rule, _ in fee_decisions:
                conn.execute(
                    "INSERT INTO transfer_risk_flags(transfer_id, rule_id) VALUES (%s, %s)",
                    (fee_transfer["id"], rule["id"]),
                )
            if held:
                _reserve_held_transfer(
                    conn,
                    fee_transfer["id"],
                    sender["id"],
                    system_accounts["hold"],
                    fee_amount,
                )
            else:
                _post_ledger(
                    conn,
                    fee_transfer["id"],
                    sender["id"],
                    system_accounts["fee_revenue"],
                    fee_amount,
                )
                conn.execute(
                    "UPDATE transfers SET status = 'completed', completed_at = now() WHERE id = %s",
                    (fee_transfer["id"],),
                )
        response = {
            "transfer_id": transfer["id"],
            "status": status,
            "amount_paise": body.amount_paise,
            "created_at": transfer["created_at"].isoformat(),
            "risk_flags": [rule["name"] for rule, _ in all_decisions.values()],
        }
        if fee_transfer:
            response["fee_paise"] = fee_amount
            response["fee_transfer_id"] = fee_transfer["id"]
        status_code = 202 if held else 201
        save_idempotent_response(conn, key_id, status_code, response)
        return status_code, response


@app.post("/users/{user_id}/transfers")
@app.post("/transfers")
def transfer(
    user_id: int,
    body: Movement,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    return movement_response(user_id, body, idempotency_key, "peer")


@app.post("/users/{user_id}/top-ups")
@app.post("/transfers/topup")
def top_up(
    user_id: int,
    body: WalletMovement,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    return movement_response(user_id, body, idempotency_key, "top_up")


@app.post("/users/{user_id}/withdrawals")
@app.post("/transfers/withdraw")
def withdraw(
    user_id: int,
    body: WalletMovement,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    return movement_response(user_id, body, idempotency_key, "withdrawal")


@app.post("/users/{user_id}/transfers/{transfer_id}/reverse", status_code=201)
@app.post("/transfers/{transfer_id}/reverse", status_code=201)
def reverse_transfer(
    user_id: int,
    transfer_id: int,
    body: ReverseRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    key = require_key(idempotency_key)
    payload = {"type": "reversal", "original_transfer_id": transfer_id, **body.model_dump()}
    with connection() as conn, conn.transaction():
        key_id, replay = replay_or_start(conn, user_id, key, payload)
        if replay:
            return idempotent_response(replay[0], replay[1])
        _user_exists(conn, user_id)
        conn.execute("SELECT set_config('app.changed_by', %s, true)", (f"user:{user_id}",))
        original = conn.execute(
            "SELECT * FROM transfers WHERE id = %s FOR UPDATE", (transfer_id,)
        ).fetchone()
        if not original:
            raise ApiError(404, "TRANSFER_NOT_FOUND", "Transfer was not found.")
        owners = conn.execute(
            "SELECT user_id FROM accounts WHERE id = ANY(%s)",
            ([original["sender_account_id"], original["recipient_account_id"]],),
        ).fetchall()
        if not any(owner["user_id"] == user_id for owner in owners):
            raise ApiError(403, "ACCOUNT_ACCESS_DENIED", "This transfer does not involve one of the user's wallets.")
        if original["transfer_type"] == "fee":
            raise ApiError(
                409,
                "FEE_REVERSAL_REQUIRES_PEER_REVERSAL",
                "A peer transfer and its fee must be reversed together.",
            )
        if original["status"] != "completed":
            raise ApiError(409, "TRANSFER_NOT_REVERSIBLE", "Only completed transfers can be reversed.")
        already_reversed = conn.execute(
            "SELECT 1 FROM transfers WHERE reversal_of = %s", (transfer_id,)
        ).fetchone()
        if already_reversed:
            raise ApiError(409, "ALREADY_REVERSED", "This transfer has already been reversed.")
        sender_id = original["recipient_account_id"]
        recipient_id = original["sender_account_id"]
        hold_account = conn.execute(
            "SELECT id FROM accounts WHERE user_id IS NULL AND account_type = 'hold'"
        ).fetchone()
        if not hold_account:
            raise ApiError(500, "SYSTEM_ACCOUNT_MISSING", "Required hold account is missing.")
        original_fee = None
        if original["transfer_type"] == "peer":
            original_fee = conn.execute(
                "SELECT * FROM transfers WHERE fee_for_transfer_id = %s",
                (original["id"],),
            ).fetchone()
        account_ids = [sender_id, recipient_id, hold_account["id"]]
        if original_fee:
            account_ids.append(original_fee["recipient_account_id"])
        accounts = _account_rows_locked(conn, account_ids)
        sender = accounts[sender_id]
        recipient = accounts[recipient_id]
        if sender["account_type"] == "wallet" and sender["balance_paise"] < original["amount_paise"]:
            raise ApiError(409, "INSUFFICIENT_FUNDS", "The original recipient no longer has enough funds.")
        risk_wallet_id = sender["id"] if sender["account_type"] == "wallet" else recipient["id"]
        decisions = _risk_decisions(
            conn, risk_wallet_id, recipient["id"], original["amount_paise"], "reversal"
        )
        held = any(hold for _, hold in decisions)
        reversal = conn.execute(
            """
            INSERT INTO transfers(
                transfer_type, status, amount_paise, note, sender_account_id,
                recipient_account_id, reversal_of
            )
            VALUES ('reversal', %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (
                "held" if held else "processing",
                original["amount_paise"], body.note or f"Reversal of transfer {transfer_id}",
                sender_id, recipient_id, transfer_id,
            ),
        ).fetchone()
        for rule, _ in decisions:
            conn.execute(
                "INSERT INTO transfer_risk_flags(transfer_id, rule_id) VALUES (%s, %s)",
                (reversal["id"], rule["id"]),
            )
        if not held:
            _post_ledger(conn, reversal["id"], sender_id, recipient_id, original["amount_paise"])
            conn.execute(
                "UPDATE transfers SET status = 'completed', completed_at = now() WHERE id = %s",
                (reversal["id"],),
            )
        else:
            _reserve_held_transfer(
                conn, reversal["id"], sender_id, hold_account["id"], original["amount_paise"]
            )

        reversed_fee = None
        if original_fee:
            fee_reversal_sender = original_fee["recipient_account_id"]
            fee_reversal_recipient = original_fee["sender_account_id"]
            fee_risk_decisions = _risk_decisions(
                conn,
                risk_wallet_id,
                fee_reversal_recipient,
                original_fee["amount_paise"],
                "reversal",
            )
            fee_held = any(hold for _, hold in fee_risk_decisions)
            reversed_fee = conn.execute(
                """
                INSERT INTO transfers(
                    transfer_type, status, amount_paise, note, sender_account_id,
                    recipient_account_id, reversal_of
                )
                VALUES ('reversal', %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    "held" if fee_held else "processing",
                    original_fee["amount_paise"],
                    f"Fee refund for transfer {original['id']}",
                    fee_reversal_sender,
                    fee_reversal_recipient,
                    original_fee["id"],
                ),
            ).fetchone()
            for rule, _ in fee_risk_decisions:
                conn.execute(
                    "INSERT INTO transfer_risk_flags(transfer_id, rule_id) VALUES (%s, %s)",
                    (reversed_fee["id"], rule["id"]),
                )
            if fee_held:
                _reserve_held_transfer(
                    conn,
                    reversed_fee["id"],
                    fee_reversal_sender,
                    hold_account["id"],
                    original_fee["amount_paise"],
                )
            else:
                _post_ledger(
                    conn,
                    reversed_fee["id"],
                    fee_reversal_sender,
                    fee_reversal_recipient,
                    original_fee["amount_paise"],
                )
                conn.execute(
                    "UPDATE transfers SET status = 'completed', completed_at = now() WHERE id = %s",
                    (reversed_fee["id"],),
                )
        response = {
            "transfer_id": reversal["id"],
            "reversal_of": transfer_id,
            "status": "held" if held else "completed",
            "amount_paise": original["amount_paise"],
            "created_at": reversal["created_at"].isoformat(),
            "risk_flags": [rule["name"] for rule, _ in decisions],
        }
        if reversed_fee:
            response["fee_refund_transfer_id"] = reversed_fee["id"]
        status_code = 202 if held else 201
        save_idempotent_response(conn, key_id, status_code, response)
        return idempotent_response(status_code, response)


def movement_response(user_id, body, idempotency_key, transfer_type):
    key = require_key(idempotency_key)
    result = perform_movement(user_id, key, body, transfer_type)
    return idempotent_response(result[0], result[1])


@app.get("/users/{user_id}/wallets/{account_id}/balance")
@app.get("/wallets/{account_id}/balance")
def balance(user_id: int, account_id: int):
    with connection() as conn:
        row = conn.execute(
            "SELECT balance_paise FROM accounts WHERE id = %s AND user_id = %s AND account_type = 'wallet'",
            (account_id, user_id),
        ).fetchone()
    if not row:
        raise ApiError(404, "WALLET_NOT_FOUND", "Wallet was not found.")
    return {"account_id": account_id, "balance_paise": row["balance_paise"], "currency": "INR"}


@app.get("/users/{user_id}/wallets/{account_id}/statement")
@app.get("/wallets/{account_id}/statement")
def statement(user_id: int, account_id: int, limit: int = 50, offset: int = 0, before_id: int | None = None):
    if not 1 <= limit <= 200 or offset < 0:
        raise ApiError(422, "INVALID_PAGINATION", "Limit must be 1-200 and offset cannot be negative.")
    with connection() as conn:
        wallet = conn.execute(
            "SELECT id FROM accounts WHERE id = %s AND user_id = %s AND account_type = 'wallet'",
            (account_id, user_id),
        ).fetchone()
        if not wallet:
            raise ApiError(404, "WALLET_NOT_FOUND", "Wallet was not found.")
        if before_id is None:
            rows = conn.execute(
                """
                SELECT l.id AS entry_id, l.transfer_id, l.direction, l.amount_paise, l.created_at,
                       t.transfer_type, t.status, t.note
                FROM ledger_entries l JOIN transfers t ON t.id = l.transfer_id
                WHERE l.account_id = %s
                ORDER BY l.created_at DESC, l.id DESC LIMIT %s OFFSET %s
                """,
                (account_id, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT l.id AS entry_id, l.transfer_id, l.direction, l.amount_paise, l.created_at,
                       t.transfer_type, t.status, t.note
                FROM ledger_entries l JOIN transfers t ON t.id = l.transfer_id
                WHERE l.account_id = %s AND (l.created_at, l.id) < (
                    SELECT created_at, id FROM ledger_entries WHERE id = %s AND account_id = %s
                )
                ORDER BY l.created_at DESC, l.id DESC LIMIT %s
                """,
                (account_id, before_id, account_id, limit),
            ).fetchall()
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return {"items": rows, "limit": limit, "offset": offset if before_id is None else None}


@app.post("/users/{user_id}/statement/ask")
def ask_statement(
    user_id: int,
    body: Question,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    key = require_key(idempotency_key)
    with connection() as conn, conn.transaction():
        key_id, replay = replay_or_start(conn, user_id, key, body.model_dump())
        if replay:
            return idempotent_response(replay[0], replay[1])
        _user_exists(conn, user_id)
        parsed = get_statement_parser().parse(body.question)
        allowed_types = {"sent", "received", "largest", "count"}
        if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str) or parsed["type"] not in allowed_types:
            raise ApiError(422, "UNSUPPORTED_QUESTION", "I can answer sent, received, largest, or transfer-count questions.")
        if parsed.get("counterparty") is not None and (
            not isinstance(parsed["counterparty"], str) or len(parsed["counterparty"]) > 100
        ):
            raise ApiError(422, "UNSUPPORTED_QUESTION", "The counterparty name is not supported.")
        try:
            start = date.fromisoformat(parsed["from"])
            end = date.fromisoformat(parsed["to"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ApiError(422, "UNSUPPORTED_QUESTION", "The question needs a valid date range.") from exc
        if end < start:
            raise ApiError(422, "INVALID_DATE_RANGE", "The end date must be on or after the start date.")
        end_exclusive = end + timedelta(days=1)
        params: list[Any] = [user_id, start, end_exclusive]
        type_filter = ""
        if parsed["type"] in ("sent", "received"):
            direction = "debit" if parsed["type"] == "sent" else "credit"
            type_filter = "AND own.direction = %s"
            params.append(direction)
        if parsed.get("counterparty"):
            type_filter += """
              AND EXISTS (
                  SELECT 1 FROM ledger_entries other
                  JOIN accounts other_account ON other_account.id = other.account_id
                  JOIN users other_user ON other_user.id = other_account.user_id
                  WHERE other.transfer_id = own.transfer_id
                    AND other.account_id <> own.account_id
                    AND lower(other_user.name) = lower(%s)
              )
            """
            params.append(parsed["counterparty"])

        order = (
            "ORDER BY t.amount_paise DESC, t.id DESC"
            if parsed["type"] == "largest"
            else "ORDER BY t.id"
        )
        limit = "LIMIT 1" if parsed["type"] == "largest" else ""

        rows = conn.execute(
            f"""
            SELECT DISTINCT t.id AS transfer_id, t.amount_paise, t.created_at, t.transfer_type
            FROM ledger_entries own
            JOIN accounts a ON a.id = own.account_id
            JOIN transfers t ON t.id = own.transfer_id
            WHERE a.user_id = %s AND t.status = 'completed'
              AND t.created_at >= %s AND t.created_at < %s
              {type_filter}
              {order} {limit}
            """,
            params,
        ).fetchall()
        total = sum(row["amount_paise"] for row in rows) if parsed["type"] in ("sent", "received") else None
        answer = {
            "type": parsed["type"],
            "total_paise": total,
            "count": len(rows) if parsed["type"] == "count" else None,
            "transfers": [
                {"transfer_id": row["transfer_id"], "amount_paise": row["amount_paise"]}
                for row in rows
            ],
        }
        conn.execute(
            "INSERT INTO statement_answers(user_id, question, answer) VALUES (%s, %s, %s)",
            (user_id, body.question, Jsonb(answer)),
        )
        save_idempotent_response(conn, key_id, 200, answer)
        return idempotent_response(200, answer)


def require_admin(authorization: str | None):
    expected = f"Bearer {os.getenv('ADMIN_TOKEN', 'local-admin-token')}"
    if authorization != expected:
        raise ApiError(401, "UNAUTHORIZED", "A valid admin bearer token is required.")


@app.post("/admin/reconciliation/run")
def reconcile_now(
    authorization: str | None = Header(default=None),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    require_admin(authorization)
    key = require_key(idempotency_key)
    with connection() as conn, conn.transaction():
        key_id, replay = replay_or_start(conn, None, key, {"operation": "reconciliation"})
        if replay:
            return idempotent_response(replay[0], replay[1])
        result = run_reconciliation()
        if result is None:
            raise ApiError(409, "JOB_ALREADY_RUNNING", "A reconciliation is already running.")
        response = result
        status_code = 202
        save_idempotent_response(conn, key_id, status_code, response)
        return idempotent_response(status_code, response)


@app.get("/admin/reconciliation/runs/{run_id}")
def reconciliation_report(run_id: int, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    with connection() as conn:
        run = conn.execute(
            "SELECT * FROM reconciliation_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        if not run:
            raise ApiError(404, "RECONCILIATION_NOT_FOUND", "Reconciliation run was not found.")
        problems = conn.execute(
            """
            SELECT check_name, entity_id, expected, actual
            FROM reconciliation_problems WHERE run_id = %s ORDER BY id
            """,
            (run_id,),
        ).fetchall()
    run["started_at"] = run["started_at"].isoformat()
    run["finished_at"] = run["finished_at"].isoformat() if run["finished_at"] else None
    return {"run": run, "problems": problems}


@app.get("/admin/reconciliation/runs")
def reconciliation_runs(
    authorization: str | None = Header(default=None),
    limit: int = 50,
    offset: int = 0,
):
    require_admin(authorization)
    if not 1 <= limit <= 200 or offset < 0:
        raise ApiError(422, "INVALID_PAGINATION", "Limit must be 1-200 and offset cannot be negative.")
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, finished_at, status, problem_count
            FROM reconciliation_runs
            ORDER BY started_at DESC, id DESC LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()
    for row in rows:
        row["started_at"] = row["started_at"].isoformat()
        row["finished_at"] = row["finished_at"].isoformat() if row["finished_at"] else None
    return {"items": rows, "limit": limit, "offset": offset}


@app.get("/admin/risk/queue")
def risk_queue(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT t.id AS transfer_id, t.transfer_type, t.status, t.amount_paise, t.note,
                   t.created_at, t.sender_account_id, t.recipient_account_id,
                   COALESCE(
                       jsonb_agg(jsonb_build_object(
                           'rule', r.name, 'action', r.action,
                           'explanation', f.explanation, 'label', f.label
                       )) FILTER (WHERE f.id IS NOT NULL),
                       '[]'::jsonb
                   ) AS risk_flags
            FROM transfers t
            LEFT JOIN transfer_risk_flags f ON f.transfer_id = t.id
            LEFT JOIN risk_rules r ON r.id = f.rule_id
            WHERE (t.status = 'held' OR f.id IS NOT NULL)
              AND t.fee_for_transfer_id IS NULL
            GROUP BY t.id
            ORDER BY t.created_at DESC, t.id DESC
            LIMIT 200
            """
        ).fetchall()
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return {"items": rows}


@app.post("/admin/transfers/{transfer_id}/release")
@app.post("/admin/risk/{transfer_id}/decision")
def release_transfer(
    transfer_id: int,
    body: TransferDecision = TransferDecision(action="release"),
    authorization: str | None = Header(default=None),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    require_admin(authorization)
    key = require_key(idempotency_key)
    with connection() as conn, conn.transaction():
        key_id, replay = replay_or_start(
            conn, None, key,
            {"operation": "decision", "transfer_id": transfer_id, "action": body.action},
        )
        if replay:
            return idempotent_response(replay[0], replay[1])
        conn.execute("SELECT set_config('app.changed_by', 'admin', true)")
        transfer_row = conn.execute(
            "SELECT * FROM transfers WHERE id = %s FOR UPDATE", (transfer_id,)
        ).fetchone()
        if not transfer_row:
            raise ApiError(404, "TRANSFER_NOT_FOUND", "Transfer was not found.")
        if transfer_row["fee_for_transfer_id"] is not None:
            transfer_row = conn.execute(
                "SELECT * FROM transfers WHERE id = %s FOR UPDATE",
                (transfer_row["fee_for_transfer_id"],),
            ).fetchone()
        fee_transfer = None
        if transfer_row["transfer_type"] == "peer":
            fee_transfer = conn.execute(
                """
                SELECT * FROM transfers
                WHERE fee_for_transfer_id = %s AND status = 'held'
                FOR UPDATE
                """,
                (transfer_row["id"],),
            ).fetchone()
        if transfer_row["status"] != "held":
            raise ApiError(409, "TRANSFER_NOT_HELD", "Only held transfers can be released.")
        hold_account = conn.execute(
            "SELECT id FROM accounts WHERE user_id IS NULL AND account_type = 'hold'"
        ).fetchone()
        if not hold_account:
            raise ApiError(500, "SYSTEM_ACCOUNT_MISSING", "Required hold account is missing.")
        account_ids = [
            transfer_row["sender_account_id"],
            transfer_row["recipient_account_id"],
            hold_account["id"],
        ]
        if fee_transfer:
            account_ids.extend(
                [fee_transfer["sender_account_id"], fee_transfer["recipient_account_id"]]
            )
        _account_rows_locked(conn, account_ids)
        if body.action == "reverse":
            _settle_held_transfer(conn, transfer_row, hold_account["id"], "reject")
            if fee_transfer:
                _settle_held_transfer(conn, fee_transfer, hold_account["id"], "reject")
            conn.execute(
                "UPDATE transfers SET status = 'rejected' WHERE id = ANY(%s)",
                ([transfer_row["id"], *([fee_transfer["id"]] if fee_transfer else [])],),
            )
            response = {"transfer_id": transfer_row["id"], "status": "rejected"}
            if fee_transfer:
                response["fee_transfer_id"] = fee_transfer["id"]
            save_idempotent_response(conn, key_id, 200, response)
            return idempotent_response(200, response)
        _settle_held_transfer(conn, transfer_row, hold_account["id"], "release")
        if fee_transfer:
            _settle_held_transfer(conn, fee_transfer, hold_account["id"], "release")
        conn.execute(
            "UPDATE transfers SET status = 'completed', completed_at = now() WHERE id = ANY(%s)",
            ([transfer_row["id"], *([fee_transfer["id"]] if fee_transfer else [])],),
        )
        response = {"transfer_id": transfer_row["id"], "status": "completed"}
        if fee_transfer:
            response["fee_transfer_id"] = fee_transfer["id"]
        save_idempotent_response(conn, key_id, 200, response)
        return idempotent_response(200, response)


@app.get("/transfers/{transfer_id}")
def transfer_detail(
    transfer_id: int,
    user_id: int,
):
    with connection() as conn:
        transfer_row = conn.execute(
            """
            SELECT t.id, t.transfer_type, t.status, t.amount_paise, t.note,
                   t.created_at, t.completed_at, t.sender_account_id,
                   t.recipient_account_id, t.reversal_of
            FROM transfers t
            WHERE t.id = %s AND (
                EXISTS (
                    SELECT 1 FROM accounts a
                    WHERE a.id = t.sender_account_id AND a.user_id = %s
                )
                OR EXISTS (
                    SELECT 1 FROM accounts a
                    WHERE a.id = t.recipient_account_id AND a.user_id = %s
                )
            )
            """,
            (transfer_id, user_id, user_id),
        ).fetchone()
        if not transfer_row:
            raise ApiError(404, "TRANSFER_NOT_FOUND", "Transfer was not found.")
        entries = conn.execute(
            """
            SELECT id, account_id, direction, amount_paise, created_at
            FROM ledger_entries WHERE transfer_id = %s ORDER BY id
            """,
            (transfer_id,),
        ).fetchall()
        audit = conn.execute(
            """
            SELECT old_status, new_status, changed_by, changed_at
            FROM transfer_status_audit WHERE transfer_id = %s ORDER BY id
            """,
            (transfer_id,),
        ).fetchall()
    for row in (transfer_row, *entries, *audit):
        if row.get("created_at"):
            row["created_at"] = row["created_at"].isoformat()
        if row.get("completed_at"):
            row["completed_at"] = row["completed_at"].isoformat()
        if row.get("changed_at"):
            row["changed_at"] = row["changed_at"].isoformat()
    return {"transfer": transfer_row, "entries": entries, "status_history": audit}


@app.get("/health")
def health():
    with connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}
