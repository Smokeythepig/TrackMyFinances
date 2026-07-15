import base64
import requests

BRIDGE = "https://bridge.simplefin.org"


def claim_setup_token(token: str) -> str:
    """Exchange a one-time SimpleFIN setup token for a permanent access URL.

    Accepts any of the forms the Bridge hands out: a base64 setup token
    (decodes to a claim URL), a raw claim URL, or a bare hex claim token.
    """
    token = token.strip()
    if token.lower().startswith("http"):
        claim_url = token
    else:
        decoded = ""
        try:
            decoded = base64.b64decode(token, validate=True).decode()
        except Exception:
            pass
        if decoded.startswith("http"):
            claim_url = decoded
        elif token and all(c in "0123456789abcdefABCDEF" for c in token):
            claim_url = f"{BRIDGE}/simplefin/claim/{token}"
        else:
            raise ValueError("Unrecognized setup token format — paste the token from SimpleFIN Bridge → My Account.")
    resp = requests.post(claim_url, timeout=30)
    resp.raise_for_status()
    access_url = resp.text.strip()
    if not access_url.startswith("http"):
        raise ValueError(f"Claim did not return an access URL: {access_url[:80]}")
    return access_url


def get_accounts(access_url: str, start_ts: int | None = None) -> dict:
    params = {"pending": 1}
    if start_ts:
        params["start-date"] = start_ts
    resp = requests.get(access_url.rstrip("/") + "/accounts", params=params, timeout=90)
    resp.raise_for_status()
    return resp.json()
