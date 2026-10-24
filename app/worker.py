import logging
import os

import psycopg
from apscheduler.schedulers.blocking import BlockingScheduler
from psycopg.rows import dict_row

from app.reconciliation import run_reconciliation
from app.statement_llm import get_risk_explainer


logging.basicConfig(level=logging.INFO)


def explain_flags():
    database_url = os.getenv("DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet")
    with psycopg.connect(database_url, row_factory=dict_row) as conn, conn.transaction():
        conn.execute("SELECT job_name FROM job_locks WHERE job_name = 'risk_explanations' FOR UPDATE NOWAIT")
        explainer = get_risk_explainer()
        while True:
            flags = conn.execute(
                """
                SELECT f.id, f.transfer_id, r.name AS rule_name, t.sender_account_id
                FROM transfer_risk_flags f
                JOIN risk_rules r ON r.id = f.rule_id
                JOIN transfers t ON t.id = f.transfer_id
                WHERE f.explanation IS NULL
                ORDER BY f.id LIMIT 1000
                """,
            ).fetchall()
            if not flags:
                break
            for flag in flags:
                recent = conn.execute(
                    """
                    SELECT id, transfer_type, amount_paise, status, note, created_at
                    FROM transfers WHERE sender_account_id = %s ORDER BY created_at DESC LIMIT 10
                    """,
                    (flag["sender_account_id"],),
                ).fetchall()
                recent_payload = [
                    {
                        **row,
                        "created_at": row["created_at"].isoformat(),
                    }
                    for row in recent
                ]
                explanation, label = explainer.explain(flag["rule_name"], recent_payload)
                conn.execute(
                    """
                    UPDATE transfer_risk_flags
                    SET explanation = %s, label = %s WHERE id = %s
                    """,
                    (explanation, label, flag["id"]),
                )


def main():
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_reconciliation, "cron", hour=0, minute=0, id="nightly_reconciliation")
    scheduler.add_job(explain_flags, "cron", hour=0, minute=15, id="risk_explanations")
    logging.info("Wallet worker started; nightly jobs scheduled in UTC.")
    scheduler.start()


if __name__ == "__main__":
    main()
