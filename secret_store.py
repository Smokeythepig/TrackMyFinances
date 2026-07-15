"""Credential storage backed by the macOS Keychain.

The SimpleFIN access URL is a bank credential, so it belongs in the Keychain
(encrypted at rest, gated by the login session) rather than in the SQLite file.
On non-macOS systems, or if the `security` tool fails, callers fall back to the
database (chmod 600).
"""
import shutil
import subprocess

SERVICE = "TrackMyFinances"


def available() -> bool:
    return shutil.which("security") is not None


def get_secret(name: str) -> str | None:
    if not available():
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        value = r.stdout.strip()
        return value if r.returncode == 0 and value else None
    except Exception:
        return None


def set_secret(name: str, value: str) -> bool:
    """Store (or update) a secret. Returns True only if it reads back correctly."""
    if not available():
        return False
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", name, "-w", value],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0 and get_secret(name) == value
    except Exception:
        return False


def delete_secret(name: str) -> None:
    if not available():
        return
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", SERVICE, "-a", name],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass
