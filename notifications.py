"""Native macOS notification delivery + dedupe log.

Every alert has a dedupe_key; once a key is in notif_log it never fires again.
Rules that scan history seed their keys silently on first run (sent=0) so a
fresh install doesn't spam about old events.
"""
import json
import shutil
import subprocess


def deliverable() -> bool:
    return shutil.which("osascript") is not None


last_error = ""


def notify(title: str, body: str) -> bool:
    """Fire a macOS notification. Returns True if the command succeeded."""
    global last_error
    if not deliverable():
        last_error = "osascript not found"
        return False
    # ensure_ascii=False: AppleScript can't parse \uXXXX escapes (emoji, dashes)
    def q(s):
        return json.dumps(str(s), ensure_ascii=False)
    script = (
        f"display notification {q(body)} "
        f"with title {q('TrackMyFinances')} "
        f"subtitle {q(title)} sound name \"Glass\""
    )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        last_error = r.stderr.strip() if r.returncode != 0 else ""
        return r.returncode == 0
    except Exception as e:
        last_error = str(e)
        return False


def already_sent(db, key: str) -> bool:
    return db.execute("SELECT 1 FROM notif_log WHERE dedupe_key=?", (key,)).fetchone() is not None


def record(db, key: str, title: str = "", body: str = "", sent: bool = True):
    db.execute(
        "INSERT OR IGNORE INTO notif_log (dedupe_key, title, body, sent) VALUES (?,?,?,?)",
        (key, title, body, 1 if sent else 0),
    )
