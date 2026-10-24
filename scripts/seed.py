
import argparse
import os

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://wallet:wallet@localhost:5432/wallet",
)


def main(target_transfers=100000):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.transaction():
            # Create exactly the required seed-user identities if missing.
            conn.execute(
                """
                INSERT INTO users(name, email, phone)
                SELECT
                    'Seed User ' || n,
                    'seed' || n || '@example.test',
                    '+1555' || lpad(n::text, 7, '0')
                FROM generate_series(1, 1000) AS n
                ON CONFLICT (email) DO NOTHING
                """
            )

            # Create exactly one wallet of each name per seed user.
            conn.execute(
                """
                INSERT INTO accounts(user_id, name, account_type)
                SELECT u.id, wallet.name, 'wallet'
                FROM users u
                CROSS JOIN (
                    VALUES ('Main wallet'), ('Savings wallet')
                ) AS wallet(name)
                WHERE u.email LIKE 'seed%@example.test'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM accounts a
                      WHERE a.user_id = u.id
                        AND a.name = wallet.name
                        AND a.account_type = 'wallet'
                  )
                """
            )
            # Only count transfers created by this seed script.
            existing_transfers = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM transfers
                WHERE note = 'seed data'
                """
            ).fetchone()["count"]

            if existing_transfers >= target_transfers:
                print(
                    f"Found {existing_transfers:,} seed transfers; "
                    "nothing to do."
                )
                return

            for start in range(
                existing_transfers + 1,
                target_transfers + 1,
                10000,
            ):
                end = min(start + 9999, target_transfers)

                conn.execute(
                    """
                    WITH seed_wallets AS (
                        SELECT
                            a.id,
                            row_number() OVER (ORDER BY a.id) AS n
                        FROM accounts a
                        JOIN users u ON u.id = a.user_id
                        WHERE u.email LIKE 'seed%%@example.test'
                          AND a.account_type = 'wallet'
                          AND a.name IN ('Main wallet', 'Savings wallet')
                    ),
                    cash_in AS (
                        SELECT id
                        FROM accounts
                        WHERE user_id IS NULL
                          AND account_type = 'cash_in'
                    ),
                    new_transfers AS (
                        INSERT INTO transfers(
                            transfer_type,
                            status,
                            amount_paise,
                            note,
                            sender_account_id,
                            recipient_account_id,
                            created_at,
                            completed_at
                        )
                        SELECT
                            'top_up',
                            'completed',
                            100 + (g.n %% 49901),
                            'seed data',
                            cash_in.id,
                            wallet.id,
                            now() - (
                                (g.n %% 86400) * interval '1 second'
                            ),
                            now()
                        FROM generate_series(
                            %s::bigint, %s::bigint
                        ) AS g(n)
                        JOIN seed_wallets wallet
                          ON wallet.n = ((g.n - 1) %% 2000) + 1
                        CROSS JOIN cash_in
                        RETURNING
                            id,
                            amount_paise,
                            sender_account_id,
                            recipient_account_id
                    )
                    INSERT INTO ledger_entries(
                        transfer_id,
                        account_id,
                        direction,
                        amount_paise
                    )
                    SELECT
                        id,
                        sender_account_id,
                        'debit',
                        amount_paise
                    FROM new_transfers
                    UNION ALL
                    SELECT
                        id,
                        recipient_account_id,
                        'credit',
                        amount_paise
                    FROM new_transfers
                    """,
                    (start, end),
                )

            # Recalculate balances only for seed wallets and the cash-in
            # account touched by this script—not every account in the DB.
            conn.execute(
                """
                UPDATE accounts a
                SET balance_paise = COALESCE((
                    SELECT SUM(
                        CASE
                            WHEN l.direction = 'credit'
                            THEN l.amount_paise
                            ELSE -l.amount_paise
                        END
                    )
                    FROM ledger_entries l
                    WHERE l.account_id = a.id
                ), 0)
                WHERE a.id IN (
                    SELECT seed_wallet.id
                    FROM accounts seed_wallet
                    JOIN users u ON u.id = seed_wallet.user_id
                    WHERE u.email LIKE 'seed%@example.test'
                      AND seed_wallet.account_type = 'wallet'
                      AND seed_wallet.name IN (
                          'Main wallet', 'Savings wallet'
                      )
                    UNION
                    SELECT id
                    FROM accounts
                    WHERE user_id IS NULL
                      AND account_type = 'cash_in'
                )
                """
            )

    print(
        f"Seeded 1,000 users, 2,000 wallets, "
        f"and {target_transfers:,} balanced transfers."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--transfers", type=int, default=100000)
    args = parser.parse_args()

    if args.transfers < 1:
        parser.error("--transfers must be positive")

    main(args.transfers)
