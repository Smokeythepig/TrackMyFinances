import csv
import hashlib
import io
import json
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, Response

from database import get_db, init_db, get_meta, set_meta
import simplefin_client
import secret_store
import notifications

SIMPLEFIN_SECRET = "simplefin_access_url"


def get_simplefin_url(db):
    """Access URL from the Keychain, migrating any legacy copy out of the DB."""
    url = secret_store.get_secret(SIMPLEFIN_SECRET)
    legacy = get_meta(db, "simplefin_access_url")
    if not url and legacy:
        if secret_store.set_secret(SIMPLEFIN_SECRET, legacy):
            db.execute("DELETE FROM app_meta WHERE key='simplefin_access_url'")
            db.commit()
        return legacy
    if url and legacy:
        # already in keychain — purge the DB copy
        db.execute("DELETE FROM app_meta WHERE key='simplefin_access_url'")
        db.commit()
    return url or legacy


def store_simplefin_url(db, access_url):
    if secret_store.set_secret(SIMPLEFIN_SECRET, access_url):
        db.execute("DELETE FROM app_meta WHERE key='simplefin_access_url'")
    else:
        # non-macOS / keychain unavailable: DB fallback (file is chmod 600)
        set_meta(db, "simplefin_access_url", access_url)
    db.commit()

app = Flask(__name__, static_folder="frontend", static_url_path="")
FRONTEND = Path(__file__).parent / "frontend"

# Hidden accounts (duplicates across providers, ignorable sub-accounts) are
# excluded from net worth and every analytic.
NOT_HIDDEN = "t.account_id NOT IN (SELECT account_id FROM hidden_accounts)"

# Investment-account activity (contributions, fund buys, dividends) is not
# cash-flow income or spending.
NOT_INVESTMENT = "t.account_id NOT IN (SELECT id FROM accounts WHERE type = 'investment')"


# Internal transfers (between your own accounts / to your own cards) must not
# count as income or spending in analytics. Provider type flags are unreliable,
# so the primary signal is a matching opposite-signed transaction in another
# visible account within 3 days.
def IS_TRANSFER(alias="t"):
    return f"""(UPPER({alias}.description) LIKE '%FUNDS TRANSFER%'
        OR UPPER({alias}.description) LIKE '%INTERNET PAYMENT - THANK YOU%'
        OR ({alias}.amount != 0 AND EXISTS (
            SELECT 1 FROM transactions pair
            JOIN accounts pa ON pa.id = pair.account_id
            WHERE pair.account_id != {alias}.account_id
              AND pair.account_id NOT IN (SELECT account_id FROM hidden_accounts)
              AND ABS(julianday(pair.date) - julianday({alias}.date)) <= 3
              AND (pair.amount = -{alias}.amount
                   -- brokerages may report contributions with the same sign as
                   -- the bank leg, so match investment legs on magnitude
                   OR (pa.type = 'investment' AND ABS(pair.amount) = ABS({alias}.amount)))
        )))"""


# Effective category = user's override if set, else the provider's category
TXN_SELECT = f"""
    SELECT t.id, t.account_id, t.description, t.amount, t.date, t.type, t.status,
           COALESCE(o.category, t.category) AS category,
           t.category AS original_category,
           (o.transaction_id IS NOT NULL) AS overridden,
           {IS_TRANSFER()} AS is_transfer,
           a.name AS account_name, a.institution_name
    FROM transactions t
    JOIN accounts a ON t.account_id = a.id
    LEFT JOIN category_overrides o ON o.transaction_id = t.id
"""


# ── Static ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(FRONTEND, "index.html")


# ── Status ──────────────────────────────────────────────────────────────────

ALLOWED_HOSTS = {"127.0.0.1:7432", "localhost:7432", "127.0.0.1", "localhost"}


@app.before_request
def reject_rebinding():
    # DNS rebinding defense: a malicious site can point its own domain at
    # 127.0.0.1 and read this API same-origin. Legit local clients always
    # send a localhost Host header.
    if request.host not in ALLOWED_HOSTS:
        return jsonify({"error": "forbidden host"}), 403


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # no external hosts, ever — blocks exfiltration even if markup injection slipped through
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'"
    )
    return resp


@app.route("/api/status")
def status():
    db = get_db()
    last_refresh = get_meta(db, "last_refresh")
    simplefin = bool(get_simplefin_url(db))
    db.close()
    return jsonify({"last_refresh": last_refresh, "simplefin": simplefin})


# ── Refresh core (shared by endpoint + background thread) ───────────────────

def compute_networth(db):
    accts = db.execute("""
        SELECT a.type, b.available, b.ledger
        FROM accounts a
        LEFT JOIN (
            SELECT account_id, available, ledger,
                   ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY fetched_at DESC) rn
            FROM balances
        ) b ON b.account_id = a.id AND b.rn = 1
        WHERE a.id NOT IN (SELECT account_id FROM hidden_accounts)
    """).fetchall()
    assets = liabilities = 0.0
    for a in accts:
        # ledger is the authoritative balance; 'available' misleads for some
        # providers (SoFi savings reports 0 available, Fidelity reports cash only)
        bal = a["ledger"] if a["ledger"] is not None else a["available"]
        if bal is None:
            continue
        if a["type"] in ("credit", "loan"):
            liabilities += abs(a["ledger"] if a["ledger"] is not None else bal)
        else:
            assets += bal
    for m in db.execute("SELECT amount, entry_type FROM manual_entries").fetchall():
        if m["entry_type"] == "asset":
            assets += m["amount"]
        else:
            liabilities += m["amount"]
    return assets, liabilities


def auto_snapshot(db):
    """Record today's net worth (one snapshot per day, latest wins)."""
    assets, liabilities = compute_networth(db)
    db.execute("DELETE FROM net_worth_snapshots WHERE snapped_at = date('now','localtime')")
    db.execute(
        "INSERT INTO net_worth_snapshots (total_assets, total_liabilities, net_worth, snapped_at)"
        " VALUES (?,?,?, date('now','localtime'))",
        (assets, liabilities, assets - liabilities),
    )


def _infer_account_type(name: str, org: str) -> str:
    n = f"{name} {org}".lower()
    if "credit" in n or "card" in n:
        return "credit"
    if "loan" in n or "mortgage" in n:
        return "loan"
    if any(k in n for k in ("invest", "brokerage", "401", "ira", "roth", "fidelity", "vanguard", "schwab")):
        return "investment"
    return "depository"


def sync_simplefin(db, errors):
    access_url = get_simplefin_url(db)
    if not access_url:
        return
    try:
        start = int(time.time()) - 400 * 86400
        data = simplefin_client.get_accounts(access_url, start)
        for err in data.get("errors", []):
            if "was capped" not in err:  # 90-day window cap is expected, not a problem
                errors.append(f"SimpleFIN: {err}")
        db.execute(
            "INSERT OR IGNORE INTO enrollments (id, institution_name, access_token) VALUES ('simplefin','SimpleFIN','')"
        )
        for acct in data.get("accounts", []):
            org = acct.get("org", {}).get("name") or acct.get("org", {}).get("domain", "")
            acct_id = "sf_" + acct["id"]
            name = acct.get("name", "")
            m = re.search(r"\((\d{4})\)", name)
            last4 = m.group(1) if m else ""
            # first time we see this account: auto-hide it if another provider
            # already covers the same underlying account (matching last four)
            is_new = not db.execute("SELECT 1 FROM accounts WHERE id=?", (acct_id,)).fetchone()
            if is_new and last4 and db.execute(
                "SELECT 1 FROM accounts WHERE enrollment_id NOT IN ('manual','simplefin') AND last_four=?",
                (last4,),
            ).fetchone():
                db.execute("INSERT OR IGNORE INTO hidden_accounts (account_id) VALUES (?)", (acct_id,))
            db.execute(
                """INSERT OR REPLACE INTO accounts
                   (id, enrollment_id, name, type, subtype, currency, institution_name, last_four)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (acct_id, "simplefin", name, _infer_account_type(name, org), "",
                 acct.get("currency", "USD"), org, last4),
            )
            avail = acct.get("available-balance")
            db.execute(
                "INSERT INTO balances (account_id, available, ledger) VALUES (?,?,?)",
                (acct_id, float(avail) if avail is not None else None, float(acct.get("balance", 0))),
            )
            for t in acct.get("transactions", []):
                posted = t.get("posted") or t.get("transacted_at")
                txn_date = datetime.fromtimestamp(posted).date().isoformat() if posted else ""
                db.execute(
                    """INSERT OR REPLACE INTO transactions
                       (id, account_id, description, amount, date, type, category, status)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    ("sf_" + str(t["id"]), acct_id, t.get("description", ""),
                     float(t.get("amount", 0)), txn_date, "simplefin", "",
                     "pending" if t.get("pending") else "posted"),
                )
    except Exception as e:
        errors.append(f"SimpleFIN: {e}")


def do_refresh():
    """Sync all accounts, balances, and transactions from SimpleFIN."""
    db = get_db()
    errors = []
    sync_simplefin(db, errors)
    auto_snapshot(db)
    set_meta(db, "last_refresh", datetime.now().isoformat(timespec="seconds"))
    db.commit()
    run_notification_checks(db, errors)
    db.commit()
    db.close()
    return errors


@app.route("/api/refresh", methods=["POST"])
def refresh_all():
    errors = do_refresh()
    return jsonify({"ok": True, "errors": errors})


# ── Accounts & Balances ──────────────────────────────────────────────────────

@app.route("/api/accounts")
def list_accounts():
    db = get_db()
    rows = db.execute("""
        SELECT a.*, b.available, b.ledger, b.fetched_at,
               (h.account_id IS NOT NULL) AS hidden
        FROM accounts a
        LEFT JOIN hidden_accounts h ON h.account_id = a.id
        LEFT JOIN (
            SELECT account_id, available, ledger, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY fetched_at DESC) rn
            FROM balances
        ) b ON b.account_id = a.id AND b.rn = 1
        ORDER BY hidden, a.institution_name, a.name
    """).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/accounts/<account_id>/hidden", methods=["PUT"])
def set_account_hidden(account_id):
    hidden = bool((request.json or {}).get("hidden"))
    db = get_db()
    if hidden:
        db.execute("INSERT OR IGNORE INTO hidden_accounts (account_id) VALUES (?)", (account_id,))
    else:
        db.execute("DELETE FROM hidden_accounts WHERE account_id=?", (account_id,))
    auto_snapshot(db)
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/accounts/<account_id>/history")
def balance_history(account_id):
    db = get_db()
    rows = db.execute("""
        SELECT date(fetched_at) AS day, available, ledger
        FROM balances WHERE account_id=?
        GROUP BY day HAVING MAX(fetched_at)
        ORDER BY day ASC
    """, (account_id,)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


# ── Transactions ─────────────────────────────────────────────────────────────

def _txn_filters():
    """Build WHERE clause + params from request args."""
    clauses, params = [], []
    if request.args.get("account_id"):
        clauses.append("t.account_id = ?")
        params.append(request.args["account_id"])
    if request.args.get("category"):
        if request.args["category"] == "uncategorized":
            clauses.append("COALESCE(o.category, t.category, '') = ''")
        else:
            clauses.append("COALESCE(o.category, t.category) = ?")
            params.append(request.args["category"])
    if request.args.get("start"):
        clauses.append("t.date >= ?")
        params.append(request.args["start"])
    if request.args.get("end"):
        clauses.append("t.date <= ?")
        params.append(request.args["end"])
    if request.args.get("q"):
        clauses.append("(LOWER(t.description) LIKE ? OR LOWER(COALESCE(o.category, t.category)) LIKE ?)")
        q = f"%{request.args['q'].lower()}%"
        params.extend([q, q])
    if request.args.get("hide_transfers"):
        clauses.append(f"NOT {IS_TRANSFER()}")
    if not request.args.get("account_id"):
        # hidden accounts stay out of the combined view, but remain reachable
        # when explicitly selected in the account filter
        clauses.append(NOT_HIDDEN)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


@app.route("/api/transactions")
def list_transactions():
    limit = request.args.get("limit", 500, type=int)
    where, params = _txn_filters()
    db = get_db()
    rows = db.execute(
        TXN_SELECT + where + " ORDER BY t.date DESC, t.id LIMIT ?",
        (*params, limit),
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/transactions/<txn_id>/category", methods=["PUT"])
def set_category(txn_id):
    category = (request.json or {}).get("category", "").strip()
    db = get_db()
    if category:
        db.execute(
            "INSERT OR REPLACE INTO category_overrides (transaction_id, category) VALUES (?,?)",
            (txn_id, category),
        )
    else:
        db.execute("DELETE FROM category_overrides WHERE transaction_id=?", (txn_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/categories")
def list_categories():
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT COALESCE(o.category, t.category) AS category
        FROM transactions t
        LEFT JOIN category_overrides o ON o.transaction_id = t.id
        WHERE COALESCE(o.category, t.category, '') != ''
        ORDER BY 1
    """).fetchall()
    db.close()
    return jsonify([r["category"] for r in rows])


@app.route("/api/export/transactions.csv")
def export_csv():
    where, params = _txn_filters()
    db = get_db()
    rows = db.execute(TXN_SELECT + where + " ORDER BY t.date DESC", params).fetchall()
    db.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    def safe(v):
        # formula-injection guard: bank-supplied text starting with =+-@ would
        # execute as a formula when the CSV opens in Excel/Numbers
        s = str(v) if v is not None else ""
        return "'" + s if s and s[0] in "=+-@" else s

    w.writerow(["date", "description", "account", "institution", "category", "amount", "type", "status"])
    for r in rows:
        w.writerow([r["date"], safe(r["description"]), safe(r["account_name"]), safe(r["institution_name"]),
                    safe(r["category"]), r["amount"], r["type"], r["status"]])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=transactions.csv"},
    )


# ── Notifications engine ─────────────────────────────────────────────────────

MAX_ALERTS_PER_RUN = 6

NOTIF_DEFAULTS = {
    "sync": True,          # bank sync failures
    "payday": True,        # payday today/tomorrow
    "paycheck": True,      # paycheck landed (+commission callout)
    "budget": True,        # budget 80% / 100%
    "large_txn": True,     # single large charge
    "large_txn_threshold": 250,
    "price_hike": True,    # subscription price increases
    "bill_due": True,      # recurring bill expected today/tomorrow
    "goal": True,          # savings goal reached
    "milestone": True,     # net worth $5k milestones
    "low_balance": True,   # checking below threshold
    "low_balance_threshold": 500,
}


def get_notif_settings(db):
    stored = get_meta(db, "notif_settings")
    s = dict(NOTIF_DEFAULTS)
    if stored:
        try:
            s.update({k: v for k, v in json.loads(stored).items() if k in NOTIF_DEFAULTS})
        except Exception:
            pass
    return s


def _fmt_usd(n):
    return f"${n:,.2f}"


def gather_alerts(db, sync_errors):
    """Evaluate every rule and return (dedupe_key, title, body) candidates."""
    s = get_notif_settings(db)
    alerts = []
    today = date.today()
    recent_cutoff = (today - timedelta(days=4)).isoformat()
    month = today.strftime("%Y-%m")

    # 1. Sync failures — stale data is the silent killer of finance trackers
    if s["sync"] and sync_errors:
        alerts.append((f"syncerr-{today.isoformat()}", "Bank sync problem",
                       f"Refresh hit an error: {str(sync_errors[0])[:120]}"))

    # Paycheck context
    paychecks = [dict(r) for r in db.execute(f"""
        SELECT t.date, t.amount FROM transactions t
        WHERE t.amount > 0 AND {PAYROLL_SQL} AND {NOT_HIDDEN} AND {NOT_INVESTMENT}
        ORDER BY t.date DESC LIMIT 30
    """).fetchall()]
    base = float(get_meta(db, "comp_base", 0) or 0)
    per_check_net = estimate_net(base) / PAY_PERIODS if base else None
    avg_check = statistics.mean([p["amount"] for p in paychecks[:8]]) if paychecks else None

    # 2. Payday tomorrow/today
    if s["payday"]:
        for payday in _next_paydays([p["date"] for p in paychecks]):
            delta = (date.fromisoformat(payday) - today).days
            if 0 <= delta <= 1:
                when = "today" if delta == 0 else "tomorrow"
                body = f"Expected {when}" + (f" — around {_fmt_usd(avg_check)}" if avg_check else "")
                alerts.append((f"payday-{payday}", f"Payday {when} 💵", body))

    # 3. Paycheck landed (with commission callout)
    if s["paycheck"]:
        for p in db.execute(f"""
            SELECT t.id, t.date, t.amount FROM transactions t
            WHERE t.amount > 0 AND {PAYROLL_SQL} AND t.date >= ? AND {NOT_HIDDEN} AND {NOT_INVESTMENT}
        """, (recent_cutoff,)).fetchall():
            body = f"{_fmt_usd(p['amount'])} deposited"
            if per_check_net and p["amount"] > per_check_net * 1.08:
                body += f" — includes ~{_fmt_usd(p['amount'] - per_check_net)} commission 🎉"
            alerts.append((f"paycheck-{p['id']}", "Paycheck landed", body))

    # 4. Budget thresholds (80% warn, 100% over)
    for b in compute_budgets(db, month) if s["budget"] else []:
        if b["monthly_limit"] <= 0:
            continue
        pct = b["spent"] / b["monthly_limit"] * 100
        if pct >= 100:
            alerts.append((f"budget-over-{b['category']}-{month}", f"Over budget: {b['category']}",
                           f"{_fmt_usd(b['spent'])} spent of {_fmt_usd(b['monthly_limit'])} — over by {_fmt_usd(b['spent'] - b['monthly_limit'])}"))
        elif pct >= 80:
            days_left = (date(today.year + (today.month == 12), today.month % 12 + 1, 1) - today).days
            alerts.append((f"budget-warn-{b['category']}-{month}", f"Budget warning: {b['category']}",
                           f"{pct:.0f}% used with {days_left} days left in the month"))

    # 5. Large new charge
    if s["large_txn"]:
        threshold = float(s["large_txn_threshold"])
        for t in db.execute(f"""
            SELECT t.id, t.description, t.amount FROM transactions t
            WHERE t.amount <= ? AND t.date >= ? AND NOT {IS_TRANSFER()} AND {NOT_HIDDEN} AND {NOT_INVESTMENT}
        """, (-threshold, recent_cutoff)).fetchall():
            alerts.append((f"txn-{t['id']}", "Large charge",
                           f"{_fmt_usd(abs(t['amount']))} at {t['description'][:60]}"))

    recurring_items, _ = compute_recurring(db)
    for r in recurring_items:
        # 6. Subscription price hikes (fixed-amount subs only)
        if (s["price_hike"] and not r["manual"] and not r["variable"] and r["active"] and not r["ignored"]
                and r["prev_median"] and r["last_amount"] >= r["prev_median"] * 1.12
                and r["last_amount"] - r["prev_median"] >= 1
                and r["last_date"] >= recent_cutoff):
            alerts.append((f"hike-{r['merchant_key']}-{r['last_amount']}", "Subscription price increase",
                           f"{r['description'][:50]}: {_fmt_usd(r['prev_median'])} → {_fmt_usd(r['last_amount'])}"))
        # 7. Bill expected tomorrow (monthly+ cadence, non-trivial amount)
        if (s["bill_due"] and r["active"] and not r["ignored"] and r["next_expected"]
                and r["cadence"] in ("monthly", "quarterly", "yearly") and r["avg_amount"] >= 15):
            delta = (date.fromisoformat(r["next_expected"]) - today).days
            if 0 <= delta <= 1:
                alerts.append((f"bill-{r['merchant_key']}-{r['next_expected']}", "Bill due soon",
                               f"{r['description'][:50]} (~{_fmt_usd(r['avg_amount'])}) expected {'today' if delta == 0 else 'tomorrow'}"))

    # 8. Savings goal reached
    for g in db.execute("""
        SELECT g.account_id, g.target, a.name,
               (SELECT ledger FROM balances b WHERE b.account_id=g.account_id ORDER BY fetched_at DESC LIMIT 1) AS bal
        FROM goals g JOIN accounts a ON a.id=g.account_id
    """).fetchall() if s["goal"] else []:
        if g["bal"] is not None and g["bal"] >= g["target"]:
            alerts.append((f"goal-{g['account_id']}-{int(g['target'])}", "Goal reached 🎉",
                           f"{g['name']} hit {_fmt_usd(g['target'])} (now {_fmt_usd(g['bal'])})"))

    # 9. Net worth milestones ($5k steps)
    if s["milestone"]:
        assets, liabilities = compute_networth(db)
        milestone = int((assets - liabilities) // 5000) * 5000
        if milestone > 0:
            alerts.append((f"networth-{milestone}", "Net worth milestone 🚀",
                           f"You crossed {_fmt_usd(milestone)} — currently {_fmt_usd(assets - liabilities)}"))

    # 10. Checking balance running low (once per account per week)
    if s["low_balance"]:
        lb_threshold = float(s["low_balance_threshold"])
        week = f"{today.isocalendar().year}w{today.isocalendar().week}"
        for a in db.execute("""
            SELECT a.id, a.name,
                   (SELECT ledger FROM balances b WHERE b.account_id=a.id ORDER BY fetched_at DESC LIMIT 1) AS bal
            FROM accounts a
            WHERE a.type = 'depository' AND (LOWER(a.name) LIKE '%checking%' OR LOWER(a.subtype) LIKE '%checking%')
              AND a.id NOT IN (SELECT account_id FROM hidden_accounts)
        """).fetchall():
            if a["bal"] is not None and 0 <= a["bal"] < lb_threshold:
                alerts.append((f"lowbal-{a['id']}-{week}", "Low balance ⚠️",
                               f"{a['name']} is at {_fmt_usd(a['bal'])} (below your {_fmt_usd(lb_threshold)} threshold)"))

    return alerts


def run_notification_checks(db, sync_errors):
    try:
        candidates = gather_alerts(db, sync_errors)
        if not get_meta(db, "notif_seeded"):
            # first run: log everything silently so history doesn't spam
            for key, title, body in candidates:
                notifications.record(db, key, title, body, sent=False)
            set_meta(db, "notif_seeded", "1")
            return
        sent = 0
        for key, title, body in candidates:
            if notifications.already_sent(db, key):
                continue
            delivered = sent < MAX_ALERTS_PER_RUN and notifications.notify(title, body)
            notifications.record(db, key, title, body, sent=delivered)
            if delivered:
                sent += 1
    except Exception:
        pass  # alerting must never break a sync


@app.route("/api/notifications")
def list_notifications():
    db = get_db()
    rows = db.execute(
        "SELECT title, body, sent, created_at FROM notif_log WHERE title != '' ORDER BY created_at DESC LIMIT 30"
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/notifications/settings", methods=["GET"])
def notif_settings_get():
    db = get_db()
    s = get_notif_settings(db)
    db.close()
    return jsonify(s)


@app.route("/api/notifications/settings", methods=["PUT"])
def notif_settings_put():
    incoming = request.json or {}
    clean = {}
    for k, default in NOTIF_DEFAULTS.items():
        if k not in incoming:
            continue
        clean[k] = float(incoming[k]) if k.endswith("_threshold") else bool(incoming[k])
    db = get_db()
    merged = {**get_notif_settings(db), **clean}
    set_meta(db, "notif_settings", json.dumps(merged))
    db.commit()
    db.close()
    return jsonify(merged)


@app.route("/api/notifications/test", methods=["POST"])
def test_notification():
    ok = notifications.notify("Test notification", "Notifications are working — you'll get alerts for paydays, budgets, big charges, price hikes, and milestones.")
    return jsonify({"ok": ok, "error": notifications.last_error or None})


# ── CSV import (any bank without a SimpleFIN connection) ────────────────────

DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%b %d, %Y")


def _parse_date(s):
    s = (s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def _parse_amount(s):
    s = str(s or "").strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _pick_column(headers, *keywords, avoid=()):
    """First header containing any keyword (in keyword priority order)."""
    for kw in keywords:
        for h in headers:
            hl = h.lower()
            if kw in hl and not any(a in hl for a in avoid):
                return h
    return None


@app.route("/api/import/csv", methods=["POST"])
def import_csv():
    d = request.json
    account_name = d["account_name"].strip()
    account_type = d.get("account_type", "credit")
    sign_mode = d.get("sign", "auto")  # auto | asis | invert
    content = d["csv"]

    reader = csv.DictReader(io.StringIO(content))
    headers = reader.fieldnames or []
    date_col = _pick_column(headers, "trans. date", "transaction date", "date", avoid=("post",))
    desc_col = _pick_column(headers, "description", "payee", "merchant", "memo", "name")
    amt_col = _pick_column(headers, "amount")
    cat_col = _pick_column(headers, "category")
    if not (date_col and desc_col and amt_col):
        return jsonify({"error": f"Couldn't find date/description/amount columns in: {headers}"}), 400

    rows = []
    for r in reader:
        dt = _parse_date(r.get(date_col))
        amt = _parse_amount(r.get(amt_col))
        desc = (r.get(desc_col) or "").strip()
        if dt is None or amt is None or not desc:
            continue
        rows.append({"date": dt, "amount": amt, "description": desc,
                     "category": (r.get(cat_col) or "").strip().lower() if cat_col else ""})
    if not rows:
        return jsonify({"error": "No parsable rows found in the CSV."}), 400

    # Discover-style exports list purchases as positive numbers; bank exports
    # list debits as negative. Auto: invert when almost everything is positive.
    invert = sign_mode == "invert" or (
        sign_mode == "auto" and sum(1 for r in rows if r["amount"] > 0) / len(rows) >= 0.8
    )
    if invert:
        for r in rows:
            r["amount"] = -r["amount"]

    account_id = "manual_" + re.sub(r"[^a-z0-9]+", "_", account_name.lower()).strip("_")

    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO enrollments (id, institution_name, access_token) VALUES ('manual', 'Imported', '')"
    )
    db.execute(
        """INSERT OR REPLACE INTO accounts
           (id, enrollment_id, name, type, subtype, currency, institution_name, last_four)
           VALUES (?,?,?,?,?,?,?,?)""",
        (account_id, "manual", account_name, account_type,
         "credit_card" if account_type == "credit" else "checking", "USD", "Imported", ""),
    )

    imported = 0
    seen = defaultdict(int)
    for r in rows:
        # deterministic id so re-importing the same/overlapping export dedupes
        key = f"{r['date']}|{r['description']}|{r['amount']}"
        seen[key] += 1
        txn_id = "imp_" + hashlib.sha1(f"{account_id}|{key}|{seen[key]}".encode()).hexdigest()[:20]
        db.execute(
            """INSERT OR REPLACE INTO transactions
               (id, account_id, description, amount, date, type, category, status)
               VALUES (?,?,?,?,?,?,?,?)""",
            (txn_id, account_id, r["description"], r["amount"], r["date"],
             "imported", r["category"], "posted"),
        )
        imported += 1

    if d.get("balance") not in (None, ""):
        db.execute(
            "INSERT INTO balances (account_id, available, ledger) VALUES (?,?,?)",
            (account_id, float(d["balance"]), float(d["balance"])),
        )

    auto_snapshot(db)
    db.commit()
    db.close()
    return jsonify({"ok": True, "account_id": account_id, "imported": imported, "inverted": invert})


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def delete_account(account_id):
    db = get_db()
    row = db.execute("SELECT enrollment_id FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row or row["enrollment_id"] != "manual":
        db.close()
        return jsonify({"error": "Only imported accounts can be deleted; disconnect the institution instead."}), 400
    db.execute("DELETE FROM category_overrides WHERE transaction_id IN (SELECT id FROM transactions WHERE account_id=?)", (account_id,))
    db.execute("DELETE FROM transactions WHERE account_id=?", (account_id,))
    db.execute("DELETE FROM balances WHERE account_id=?", (account_id,))
    db.execute("DELETE FROM hidden_accounts WHERE account_id=?", (account_id,))
    db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ── SimpleFIN ────────────────────────────────────────────────────────────────

@app.route("/api/simplefin/connect", methods=["POST"])
def simplefin_connect():
    token = (request.json or {}).get("token", "").strip()
    if not token:
        return jsonify({"error": "No setup token provided."}), 400

    # Setup tokens are single-use: prove the DB is writable BEFORE claiming,
    # so a storage failure can't burn the token.
    try:
        db = get_db()
        set_meta(db, "simplefin_write_test", "ok")
        db.commit()
        db.execute("DELETE FROM app_meta WHERE key='simplefin_write_test'")
        db.commit()
    except Exception as e:
        return jsonify({"error": f"Database not writable — token NOT claimed, it is still valid. ({e})"}), 500

    try:
        access_url = simplefin_client.claim_setup_token(token)
    except Exception as e:
        db.close()
        return jsonify({"error": f"Could not claim setup token: {e}"}), 400

    store_simplefin_url(db, access_url)
    errors = []
    sync_simplefin(db, errors)
    auto_snapshot(db)
    db.commit()
    n = db.execute("SELECT COUNT(*) c FROM accounts WHERE enrollment_id='simplefin'").fetchone()["c"]
    db.close()
    return jsonify({"ok": True, "accounts": n, "errors": errors})


@app.route("/api/simplefin", methods=["DELETE"])
def simplefin_disconnect():
    secret_store.delete_secret(SIMPLEFIN_SECRET)
    db = get_db()
    db.execute("DELETE FROM app_meta WHERE key='simplefin_access_url'")
    db.execute("DELETE FROM category_overrides WHERE transaction_id IN (SELECT id FROM transactions WHERE account_id LIKE 'sf_%')")
    db.execute("DELETE FROM transactions WHERE account_id LIKE 'sf_%'")
    db.execute("DELETE FROM balances WHERE account_id LIKE 'sf_%'")
    db.execute("DELETE FROM hidden_accounts WHERE account_id LIKE 'sf_%'")
    db.execute("DELETE FROM accounts WHERE enrollment_id='simplefin'")
    db.execute("DELETE FROM enrollments WHERE id='simplefin'")
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ── Insights ─────────────────────────────────────────────────────────────────

@app.route("/api/insights/summary")
def insights_summary():
    """Current-month vs previous-month income, spending, savings rate."""
    db = get_db()
    today = date.today()
    this_month = today.strftime("%Y-%m")
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    def month_stats(month):
        row = db.execute(f"""
            SELECT
              COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0) AS income,
              COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END), 0) AS spending
            FROM transactions t WHERE substr(date, 1, 7) = ? AND NOT {IS_TRANSFER()} AND {NOT_HIDDEN} AND {NOT_INVESTMENT}
        """, (month,)).fetchone()
        income, spending = row["income"], row["spending"]
        rate = round((income - spending) / income * 100, 1) if income > 0 else None
        return {"month": month, "income": round(income, 2), "spending": round(spending, 2),
                "net": round(income - spending, 2), "savings_rate": rate}

    result = {"current": month_stats(this_month), "previous": month_stats(prev)}
    db.close()
    return jsonify(result)


@app.route("/api/insights/monthly")
def insights_monthly():
    """Income/expenses per month over the full history (server-side, not capped)."""
    months = request.args.get("months", 12, type=int)
    db = get_db()
    rows = db.execute(f"""
        SELECT substr(date, 1, 7) AS month,
               COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0) AS income,
               COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END), 0) AS expenses
        FROM transactions t
        WHERE date != '' AND NOT {IS_TRANSFER()} AND {NOT_HIDDEN} AND {NOT_INVESTMENT}
        GROUP BY month ORDER BY month DESC LIMIT ?
    """, (months,)).fetchall()
    db.close()
    return jsonify([dict(r) for r in reversed(rows)])


@app.route("/api/insights/categories")
def insights_categories():
    """Spending by category for a given month (default: current)."""
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    db = get_db()
    rows = db.execute(f"""
        SELECT COALESCE(NULLIF(COALESCE(o.category, t.category), ''), 'uncategorized') AS category,
               ROUND(SUM(-t.amount), 2) AS spent,
               COUNT(*) AS count
        FROM transactions t
        LEFT JOIN category_overrides o ON o.transaction_id = t.id
        WHERE t.amount < 0 AND substr(t.date, 1, 7) = ? AND NOT {IS_TRANSFER()} AND {NOT_HIDDEN} AND {NOT_INVESTMENT}
        GROUP BY 1 ORDER BY spent DESC
    """, (month,)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/insights/merchants")
def insights_merchants():
    """Top merchants by spend over the last N days (default 90)."""
    days = request.args.get("days", 90, type=int)
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    db = get_db()
    rows = db.execute(f"""
        SELECT description, ROUND(SUM(-amount), 2) AS spent, COUNT(*) AS count
        FROM transactions t
        WHERE amount < 0 AND date >= ? AND NOT {IS_TRANSFER()} AND {NOT_HIDDEN} AND {NOT_INVESTMENT}
        GROUP BY description ORDER BY spent DESC LIMIT 15
    """, (cutoff,)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


# ── Income ───────────────────────────────────────────────────────────────────

PAYROLL_SQL = """(UPPER(t.description) LIKE '%PAYROLL%'
    OR UPPER(t.description) LIKE '%SALARY%'
    OR UPPER(t.description) LIKE '%DIRECT DEP%')"""

# 2026 federal single-filer brackets + standard deduction; Texas has no state
# income tax, so take-home ≈ gross − federal − FICA. Estimates only (ignores
# 401k/insurance deductions and assumes single filer).
FED_BRACKETS_2026 = [
    (12_400, 0.10), (50_400, 0.12), (105_700, 0.22), (201_775, 0.24),
    (256_225, 0.32), (640_600, 0.35), (float("inf"), 0.37),
]
STD_DEDUCTION_2026 = 16_100
FICA_RATE = 0.0765  # 6.2% Social Security + 1.45% Medicare (under SS wage cap)
PAY_PERIODS = 24    # semi-monthly


def estimate_net(gross: float) -> float:
    """Estimated annual take-home for a Texas single filer."""
    taxable = max(0.0, gross - STD_DEDUCTION_2026)
    fed = 0.0
    prev = 0.0
    for cap, rate in FED_BRACKETS_2026:
        if taxable <= prev:
            break
        fed += (min(taxable, cap) - prev) * rate
        prev = cap
    return gross - fed - gross * FICA_RATE


def _next_paydays(paycheck_dates, count=2):
    """Predict upcoming paydays from the observed day-of-month pattern
    (semi-monthly pay clusters around two days; medians absorb weekend shifts)."""
    import calendar
    doms = [int(d[8:10]) for d in paycheck_dates if len(d) == 10]
    clusters = [[d for d in doms if d < 22], [d for d in doms if d >= 22]]
    pay_doms = sorted(int(statistics.median(c)) for c in clusters if c)
    if not pay_doms:
        return []
    today = date.today()
    out = []
    for delta_m in range(0, 3):
        y = today.year + (today.month - 1 + delta_m) // 12
        m = (today.month - 1 + delta_m) % 12 + 1
        last_day = calendar.monthrange(y, m)[1]
        for dom in pay_doms:
            d = date(y, m, min(dom, last_day))
            if d > today:
                out.append(d.isoformat())
    return out[:count]


@app.route("/api/income/summary")
def income_summary():
    db = get_db()
    year_start = date.today().strftime("%Y-01-01")
    paychecks = [dict(r) for r in db.execute(f"""
        SELECT t.date, t.description, t.amount FROM transactions t
        WHERE t.amount > 0 AND {PAYROLL_SQL} AND {NOT_HIDDEN} AND {NOT_INVESTMENT}
        ORDER BY t.date DESC LIMIT 60
    """).fetchall()]
    ytd_paychecks = sum(p["amount"] for p in paychecks if p["date"] >= year_start)
    ytd_total = db.execute(f"""
        SELECT COALESCE(SUM(amount), 0) s FROM transactions t
        WHERE amount > 0 AND date >= ? AND NOT {IS_TRANSFER()} AND {NOT_HIDDEN} AND {NOT_INVESTMENT}
    """, (year_start,)).fetchone()["s"]

    base = float(get_meta(db, "comp_base", 0) or 0)
    commission_target = float(get_meta(db, "comp_commission", 0) or 0)
    db.close()

    comp = None
    if base > 0:
        ote = base + commission_target
        net_base = estimate_net(base)
        net_ote = estimate_net(ote)
        per_check_net = net_base / PAY_PERIODS
        # commission dollars stack on top of base, so their net rate is marginal
        commission_net_rate = (net_ote - net_base) / commission_target if commission_target else 0.7

        # any payroll deposit meaningfully above a base check carries commission
        commission_net_ytd = sum(
            p["amount"] - per_check_net
            for p in paychecks
            if p["date"] >= year_start and p["amount"] > per_check_net * 1.08
        )
        for p in paychecks:
            p["commission"] = round(p["amount"] - per_check_net, 2) if p["amount"] > per_check_net * 1.08 else 0
        commission_gross_ytd = commission_net_ytd / commission_net_rate if commission_net_rate else 0

        elapsed = date.today().timetuple().tm_yday / 365.0
        comp = {
            "base": base,
            "commission_target": commission_target,
            "ote": ote,
            "est_net_ote": round(net_ote, 2),
            "est_net_base": round(net_base, 2),
            "effective_tax_rate": round((1 - net_ote / ote) * 100, 1) if ote else None,
            "per_check_net": round(per_check_net, 2),
            "commission_net_ytd": round(commission_net_ytd, 2),
            "commission_gross_ytd": round(commission_gross_ytd, 2),
            "commission_progress_pct": round(commission_gross_ytd / commission_target * 100, 1) if commission_target else None,
            "pace_pct": round(ytd_total / (net_ote * elapsed) * 100, 1) if net_ote and elapsed else None,
        }

    recent = [p["amount"] for p in paychecks[:8]]
    return jsonify({
        "ytd_paychecks": round(ytd_paychecks, 2),
        "ytd_total": round(ytd_total, 2),
        "ytd_other": round(ytd_total - ytd_paychecks, 2),
        "avg_paycheck": round(statistics.mean(recent), 2) if recent else None,
        "next_paydays": _next_paydays([p["date"] for p in paychecks]),
        "comp": comp,
        "paychecks": paychecks[:30],
    })


@app.route("/api/income/comp", methods=["PUT"])
def set_income_comp():
    d = request.json or {}
    db = get_db()
    set_meta(db, "comp_base", float(d.get("base") or 0))
    set_meta(db, "comp_commission", float(d.get("commission") or 0))
    db.execute("DELETE FROM app_meta WHERE key='income_target'")
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ── Savings goals ────────────────────────────────────────────────────────────

@app.route("/api/goals", methods=["GET"])
def list_goals():
    db = get_db()
    rows = db.execute("""
        SELECT g.account_id, g.target, a.name, a.institution_name,
               (SELECT ledger FROM balances b WHERE b.account_id = g.account_id
                ORDER BY fetched_at DESC LIMIT 1) AS balance
        FROM goals g JOIN accounts a ON a.id = g.account_id
        ORDER BY a.name
    """).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/goals", methods=["POST"])
def set_goal():
    d = request.json
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO goals (account_id, target) VALUES (?,?)",
        (d["account_id"], float(d["target"])),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/goals/<account_id>", methods=["DELETE"])
def delete_goal(account_id):
    db = get_db()
    db.execute("DELETE FROM goals WHERE account_id=?", (account_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ── Recurring / subscription detection ───────────────────────────────────────

def _merchant_key(desc: str) -> str:
    """Normalize a description so 'NETFLIX.COM 0123' and 'NETFLIX.COM 0456' group together."""
    s = re.sub(r"\d+", "", desc.upper())
    s = re.sub(r"[^A-Z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


CADENCES = [
    ("weekly", 5, 9, 52),
    ("biweekly", 12, 17, 26),
    ("monthly", 26, 35, 12),
    ("quarterly", 80, 100, 4),
    ("yearly", 350, 380, 1),
]


PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}


def compute_recurring(db):
    ignored = {r["merchant_key"] for r in db.execute("SELECT merchant_key FROM recurring_ignored").fetchall()}
    dismissed = {r["merchant_key"] for r in db.execute("SELECT merchant_key FROM recurring_dismissed").fetchall()}
    manual = [dict(r) for r in db.execute("SELECT * FROM manual_subscriptions ORDER BY name").fetchall()]
    rows = db.execute(
        f"SELECT description, amount, date FROM transactions t WHERE amount < 0 AND date != '' AND {NOT_HIDDEN} AND {NOT_INVESTMENT} ORDER BY date"
    ).fetchall()

    groups = defaultdict(list)
    for r in rows:
        key = _merchant_key(r["description"] or "")
        if key:
            groups[key].append(r)

    results = []
    for key, txns in groups.items():
        if len(txns) < 3 or key in dismissed:
            continue
        # judge cadence on the trailing window so old one-off history doesn't
        # mask a bill that settled into a rhythm later
        recent = txns[-6:]
        dates = sorted(datetime.strptime(t["date"], "%Y-%m-%d").date() for t in recent)
        gaps = [g for g in ((dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)) if g > 0]
        if not gaps:
            continue
        med = statistics.median(gaps)
        cadence = next((c for c in CADENCES if c[1] <= med <= c[2]), None)
        if not cadence:
            continue
        amounts = [abs(t["amount"]) for t in recent]
        avg = statistics.mean(amounts)
        if avg == 0:
            continue
        fixed = (max(amounts) - min(amounts)) / avg <= 0.35
        if not fixed:
            # variable-amount bills (utilities, card payments): only trust
            # longer cadences with regular gaps, and use the median amount
            regularity = sum(1 for g in gaps if abs(g - med) <= med * 0.4) / len(gaps)
            if cadence[0] not in ("monthly", "quarterly", "yearly") or len(txns) < 4 or regularity < 0.6:
                continue
            avg = statistics.median(amounts)
        last = dates[-1]
        # stale? if we've missed ~2 cycles, it's probably cancelled
        active = (date.today() - last).days <= cadence[2] * 2
        results.append({
            "merchant_key": key,
            "description": txns[-1]["description"],
            "cadence": cadence[0],
            "variable": not fixed,
            "avg_amount": round(avg, 2),
            "monthly_cost": round(avg * cadence[3] / 12, 2),
            "occurrences": len(txns),
            "last_date": last.isoformat(),
            "next_expected": (last + timedelta(days=int(med))).isoformat(),
            "active": active,
            "ignored": key in ignored,
            "manual": False,
            "last_amount": round(amounts[-1], 2),
            "prev_median": round(statistics.median(amounts[:-1]), 2) if len(amounts) > 1 else None,
        })

    for m in manual:
        results.append({
            "merchant_key": f"manual_{m['id']}",
            "manual_id": m["id"],
            "description": m["name"],
            "cadence": m["cadence"],
            "variable": False,
            "avg_amount": round(m["amount"], 2),
            "monthly_cost": round(m["amount"] * PER_YEAR[m["cadence"]] / 12, 2),
            "occurrences": None,
            "last_date": None,
            "next_expected": m["next_date"],
            "active": True,
            "ignored": False,
            "manual": True,
            "last_amount": None,
            "prev_median": None,
        })

    results.sort(key=lambda x: (-x["active"], -x["monthly_cost"]))
    return results, len(dismissed)


@app.route("/api/recurring")
def recurring():
    db = get_db()
    items, dismissed_count = compute_recurring(db)
    db.close()
    return jsonify({"items": items, "dismissed_count": dismissed_count})


@app.route("/api/recurring/ignore", methods=["POST"])
def recurring_ignore():
    d = request.json
    db = get_db()
    if d.get("ignored"):
        db.execute("INSERT OR REPLACE INTO recurring_ignored (merchant_key) VALUES (?)", (d["merchant_key"],))
    else:
        db.execute("DELETE FROM recurring_ignored WHERE merchant_key=?", (d["merchant_key"],))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/recurring/dismiss", methods=["POST"])
def recurring_dismiss():
    d = request.json
    db = get_db()
    if d.get("restore_all"):
        db.execute("DELETE FROM recurring_dismissed")
    else:
        db.execute("INSERT OR REPLACE INTO recurring_dismissed (merchant_key) VALUES (?)", (d["merchant_key"],))
        db.execute("DELETE FROM recurring_ignored WHERE merchant_key=?", (d["merchant_key"],))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/subscriptions", methods=["POST"])
def add_subscription():
    d = request.json
    cadence = d.get("cadence", "monthly")
    if cadence not in PER_YEAR:
        return jsonify({"error": f"cadence must be one of {list(PER_YEAR)}"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO manual_subscriptions (name, amount, cadence, next_date) VALUES (?,?,?,?)",
        (d["name"].strip(), float(d["amount"]), cadence, d.get("next_date") or None),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/subscriptions/<int:sub_id>", methods=["DELETE"])
def delete_subscription(sub_id):
    db = get_db()
    db.execute("DELETE FROM manual_subscriptions WHERE id=?", (sub_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ── Budgets ──────────────────────────────────────────────────────────────────

def compute_budgets(db, month):
    rows = db.execute(f"""
        SELECT b.category, b.monthly_limit,
               COALESCE((
                   SELECT SUM(-t.amount) FROM transactions t
                   LEFT JOIN category_overrides o ON o.transaction_id = t.id
                   WHERE t.amount < 0 AND substr(t.date, 1, 7) = ? AND NOT {IS_TRANSFER()} AND {NOT_HIDDEN} AND {NOT_INVESTMENT}
                     AND COALESCE(NULLIF(COALESCE(o.category, t.category), ''), 'uncategorized') = b.category
               ), 0) AS spent
        FROM budgets b ORDER BY b.category
    """, (month,)).fetchall()
    return [{**dict(r), "spent": round(r["spent"], 2)} for r in rows]


@app.route("/api/budgets", methods=["GET"])
def list_budgets():
    """Budgets with current-month spend per category."""
    db = get_db()
    budgets = compute_budgets(db, date.today().strftime("%Y-%m"))
    db.close()
    return jsonify(budgets)


@app.route("/api/budgets", methods=["POST"])
def set_budget():
    d = request.json
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO budgets (category, monthly_limit) VALUES (?,?)",
        (d["category"].strip(), float(d["monthly_limit"])),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/budgets/<category>", methods=["DELETE"])
def delete_budget(category):
    db = get_db()
    db.execute("DELETE FROM budgets WHERE category=?", (category,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ── Manual entries (assets/liabilities) ─────────────────────────────────────

@app.route("/api/manual", methods=["GET"])
def list_manual():
    db = get_db()
    rows = db.execute("SELECT * FROM manual_entries ORDER BY created_at DESC").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/manual", methods=["POST"])
def add_manual():
    d = request.json
    db = get_db()
    db.execute(
        "INSERT INTO manual_entries (label, amount, entry_type, category) VALUES (?,?,?,?)",
        (d["label"], d["amount"], d["entry_type"], d.get("category", "")),
    )
    auto_snapshot(db)
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/manual/<int:entry_id>", methods=["DELETE"])
def delete_manual(entry_id):
    db = get_db()
    db.execute("DELETE FROM manual_entries WHERE id=?", (entry_id,))
    auto_snapshot(db)
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ── Net worth snapshots ──────────────────────────────────────────────────────

@app.route("/api/networth/snapshot", methods=["POST"])
def snapshot_networth():
    db = get_db()
    auto_snapshot(db)
    db.commit()
    row = db.execute("SELECT * FROM net_worth_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    return jsonify(dict(row))


@app.route("/api/networth/history")
def networth_history():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM net_worth_snapshots ORDER BY snapped_at ASC"
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    init_db()
    # localhost only — never expose financial data on the network
    app.run(host="127.0.0.1", port=7432, debug=False, threaded=True)
