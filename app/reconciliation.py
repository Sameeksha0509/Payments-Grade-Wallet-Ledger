import os

import psycopg
from psycopg.rows import dict_row


def run_reconciliation():
    database_url = os.getenv("DATABASE_URL", "******localhost:5432/wallet")
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        try:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                conn.execute(
                    "SELECT job_name FROM job_locks WHERE job_name = 'nightly_reconciliation' FOR UPDATE NOWAIT"
                )
                run = conn.execute(
                    "INSERT INTO reconciliation_runs(status) VALUES ('running') RETURNING id, started_at"
                ).fetchone()
                run_id = run["id"]
                problems = []

                transfer_issues = conn.execute(
                    """
                    SELECT t.id, t.amount_paise, t.status,
                           COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'debit'), 0) AS debits,
                           COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'credit'), 0) AS credits,
                           COUNT(l.id) AS entries,
                           COUNT(l.id) FILTER (WHERE a.account_type = 'hold') AS hold_entries,
                           CASE
                               WHEN COUNT(l.id) FILTER (WHERE a.account_type = 'hold') = 2
                               THEN t.amount_paise * 2
                               ELSE t.amount_paise
                           END AS expected_total
                    FROM transfers t
                    LEFT JOIN ledger_entries l ON l.transfer_id = t.id
                    LEFT JOIN accounts a ON a.id = l.account_id
                    GROUP BY t.id
                    HAVING NOT (t.status = 'rejected' AND COUNT(l.id) = 0)
                       AND (
                           COUNT(l.id) < 2
                           OR COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'debit'), 0)
                              <> COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'credit'), 0)
                           OR COALESCE(SUM(l.amount_paise) FILTER (WHERE l.direction = 'debit'), 0)
                              <> CASE
                                     WHEN COUNT(l.id) FILTER (WHERE a.account_type = 'hold') = 2
                                     THEN t.amount_paise * 2
                                     ELSE t.amount_paise
                                 END
                           OR (
                               t.status = 'held'
                               AND COUNT(l.id) FILTER (WHERE a.account_type = 'hold') <> 1
                           )
                           OR (
                               t.status <> 'held'
                               AND COUNT(l.id) FILTER (WHERE a.account_type = 'hold') NOT IN (0, 2)
                           )
                       )
                    """
                ).fetchall()
                for item in transfer_issues:
                    problems.append((
                        "transfer_balanced",
                        item["id"],
                        f"entries>=2;balanced;total={item['expected_total']};status={item['status']}",
                        (
                            f"debits={item['debits']};credits={item['credits']};"
                            f"entries={item['entries']};hold_entries={item['hold_entries']}"
                        ),
                    ))

                account_issues = conn.execute(
                    """
                    SELECT a.id, a.balance_paise,
                           COALESCE(SUM(CASE WHEN l.direction = 'credit'
                                             THEN l.amount_paise ELSE -l.amount_paise END), 0) AS computed
                    FROM accounts a LEFT JOIN ledger_entries l ON l.account_id = a.id
                    GROUP BY a.id
                    HAVING a.balance_paise <> COALESCE(
                        SUM(CASE WHEN l.direction = 'credit'
                                 THEN l.amount_paise ELSE -l.amount_paise END), 0
                    )
                    """
                ).fetchall()
                for item in account_issues:
                    problems.append((
                        "stored_balance",
                        item["id"],
                        str(item["computed"]),
                        str(item["balance_paise"]),
                    ))

                overdrafts = conn.execute(
                    """
                    SELECT a.id, COALESCE(SUM(CASE WHEN l.direction = 'credit'
                                                   THEN l.amount_paise ELSE -l.amount_paise END), 0) AS computed
                    FROM accounts a LEFT JOIN ledger_entries l ON l.account_id = a.id
                    WHERE a.account_type = 'wallet'
                    GROUP BY a.id
                    HAVING COALESCE(SUM(CASE WHEN l.direction = 'credit'
                                             THEN l.amount_paise ELSE -l.amount_paise END), 0) < 0
                    """
                ).fetchall()
                for item in overdrafts:
                    problems.append(("no_overdraft", item["id"], ">=0", str(item["computed"])))

                missing = conn.execute(
                    """
                    SELECT t.id, COUNT(l.id) AS entries
                    FROM transfers t
                    LEFT JOIN ledger_entries l ON l.transfer_id = t.id
                    GROUP BY t.id
                    HAVING COUNT(l.id) < 2
                       AND NOT (t.status = 'rejected' AND COUNT(l.id) = 0)
                    """
                ).fetchall()
                for item in missing:
                    problems.append((
                        "transfer_has_entries", item["id"], "at least 2", str(item["entries"]),
                    ))

                totals = conn.execute(
                    """
                    SELECT COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'debit'), 0) AS debits,
                           COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'credit'), 0) AS credits
                    FROM ledger_entries
                    """
                ).fetchone()
                if totals["debits"] != totals["credits"]:
                    problems.append((
                        "books_balanced", None, str(totals["debits"]), str(totals["credits"]),
                    ))

                orphaned = conn.execute(
                    """
                    SELECT l.id FROM ledger_entries l
                    LEFT JOIN transfers t ON t.id = l.transfer_id WHERE t.id IS NULL
                    """
                ).fetchall()
                for item in orphaned:
                    problems.append(("no_orphans", item["id"], "transfer exists", "missing"))

                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO reconciliation_problems(
                            run_id, check_name, entity_id, expected, actual
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        [(run_id, *problem) for problem in problems],
                    )

                status = "issues" if problems else "passed"
                conn.execute(
                    """
                    UPDATE reconciliation_runs
                    SET finished_at = now(), status = %s, problem_count = %s WHERE id = %s
                    """,
                    (status, len(problems), run_id),
                )
                return {
                    "run_id": run_id,
                    "status": status,
                    "problem_count": len(problems),
                    "problems": [
                        {
                            "check": check,
                            "entity_id": entity_id,
                            "expected": expected,
                            "actual": actual,
                        }
                        for check, entity_id, expected, actual in problems
                    ],
                }
        except psycopg.errors.LockNotAvailable:
            return None
