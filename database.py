import sqlite3
import os
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "finances.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    os.chmod(DB_PATH.parent, 0o700)
    conn = get_db()
    conn.close()
    # financial data — owner-only access
    os.chmod(DB_PATH, 0o600)
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS enrollments (
            id TEXT PRIMARY KEY,
            institution_name TEXT NOT NULL,
            access_token TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            enrollment_id TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT,
            subtype TEXT,
            currency TEXT DEFAULT 'USD',
            institution_name TEXT,
            last_four TEXT,
            FOREIGN KEY (enrollment_id) REFERENCES enrollments(id)
        );

        CREATE TABLE IF NOT EXISTS balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            available REAL,
            ledger REAL,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            description TEXT,
            amount REAL,
            date TEXT,
            type TEXT,
            category TEXT,
            status TEXT,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS manual_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            amount REAL NOT NULL,
            entry_type TEXT NOT NULL CHECK(entry_type IN ('asset','liability')),
            category TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS net_worth_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_assets REAL,
            total_liabilities REAL,
            net_worth REAL,
            snapped_at DATE DEFAULT (date('now'))
        );

        CREATE TABLE IF NOT EXISTS budgets (
            category TEXT PRIMARY KEY,
            monthly_limit REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS category_overrides (
            transaction_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            FOREIGN KEY (transaction_id) REFERENCES transactions(id)
        );

        CREATE TABLE IF NOT EXISTS category_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT NOT NULL,
            category TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS recurring_ignored (
            merchant_key TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS recurring_dismissed (
            merchant_key TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS manual_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            cadence TEXT NOT NULL CHECK(cadence IN ('weekly','biweekly','monthly','quarterly','yearly')),
            next_date TEXT
        );

        CREATE TABLE IF NOT EXISTS hidden_accounts (
            account_id TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS excluded_transactions (
            transaction_id TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS goals (
            account_id TEXT PRIMARY KEY,
            target REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS notif_log (
            dedupe_key TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            sent INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
    """)
    # migration: track whether an override came from the user or a rule
    try:
        conn.execute("ALTER TABLE category_overrides ADD COLUMN source TEXT DEFAULT 'manual'")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_txn_amount ON transactions(amount);
        CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id);
        CREATE INDEX IF NOT EXISTS idx_bal_account ON balances(account_id, fetched_at);
    """)
    conn.commit()
    conn.close()


BACKUP_DIR = DB_PATH.parent / "backups"
BACKUP_KEEP = 14


def backup_db():
    """Consistent daily snapshot of the whole DB (data + every user customization).

    Uses SQLite's online backup API so it's safe while the app is running.
    Keeps the last BACKUP_KEEP dailies. Called from the sync path, so it runs
    at least once a day without any scheduler.
    """
    from datetime import date
    BACKUP_DIR.mkdir(exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    target = BACKUP_DIR / f"finances-{date.today().isoformat()}.db"
    if target.exists():
        return
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    os.chmod(target, 0o600)
    backups = sorted(BACKUP_DIR.glob("finances-*.db"))
    for old in backups[:-BACKUP_KEEP]:
        old.unlink()


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)", (key, str(value)))
