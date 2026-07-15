# TrackMyFinances

A local-first personal finance desktop app for macOS. All of your financial data
lives in a SQLite database **on your machine** — there is no cloud backend, no
telemetry, and no third party that ever sees your transactions besides the bank
aggregator you connect.

![Python](https://img.shields.io/badge/python-3.12-blue) ![Flask](https://img.shields.io/badge/flask-3.0-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Bank sync via [SimpleFIN Bridge](https://bridge.simplefin.org)** ($15/yr) —
  connects Discover, SoFi, Fidelity, USAA, and thousands of other institutions.
  One token pasted once; syncs on demand and every 6 hours while open.
- **Dashboard** — net worth, monthly income/spending with month-over-month deltas,
  savings rate, next-paycheck countdown, top merchants, cash-flow chart.
- **Smart transfer detection** — money moved between your own accounts (including
  credit-card payments and brokerage contributions) never counts as income or spending.
- **Transactions** — full history, search, date/category/account filters,
  click-to-recategorize, CSV export.
- **Income** — payroll detection, pre-tax compensation model (base + commission)
  with estimated take-home (federal + FICA), commission tracking against OTE,
  payday prediction.
- **Budgets** — per-category monthly limits with progress bars.
- **Recurring** — automatic subscription/bill detection (fixed and variable amounts),
  manual subscriptions, dismiss false positives.
- **Net worth** — automatic daily snapshots, history chart, savings goals per account,
  manual assets/liabilities.
- **CSV import** — for banks without a SimpleFIN connection.

## Setup

```bash
git clone https://github.com/Smokeythepig/TrackMyFinances.git
cd TrackMyFinances
./setup.sh   # creates venv, installs dependencies
./run.sh     # launches the app
```

Then, inside the app: **Accounts → paste your SimpleFIN setup token → Connect.**

To get a token: create an account at [bridge.simplefin.org](https://bridge.simplefin.org),
link your banks there, then go to **My Account → New App → Get Setup Token**.
Setup tokens are single-use — the app exchanges yours for a permanent access URL
on first connect.

To build a double-clickable macOS app: `./build.sh` (output in `dist/`).

## Security model

- **Local-only server.** The Flask backend binds to `127.0.0.1` exclusively —
  nothing is reachable from your network.
- **Your data never leaves your machine.** The app talks to exactly one external
  host: the SimpleFIN Bridge (read-only bank data, over TLS). There is no
  analytics, no error reporting, no phone-home.
- **Read-only bank access.** SimpleFIN is a read-only protocol — the credential
  the app stores cannot move money or change anything at your bank.
- **Credentials live in the macOS Keychain.** The SimpleFIN access URL is stored
  as a Keychain generic password (encrypted at rest by macOS), not in a file.
  On non-macOS systems it falls back to the SQLite database, which is created
  with owner-only (`600`) permissions inside a `700` directory. `data/` is
  gitignored — never commit it.
- **Hardened responses.** The local server sends `Content-Security-Policy`
  (no external hosts allowed at all), `X-Frame-Options: DENY`, `nosniff`, and
  `no-referrer` headers, and all bank-sourced text is HTML-escaped before render.
- **No secrets in code.** Nothing in this repository contains keys or tokens;
  each user supplies their own SimpleFIN token through the UI at runtime.

**If you fork or contribute:** never commit anything under `data/`, any `.db`,
`.pem`, or `.env` file. The `.gitignore` enforces this — leave it intact.

## Architecture

| Piece | Tech |
|---|---|
| Backend | Flask (Python 3.12), SQLite |
| Frontend | Vanilla JS + Chart.js (vendored, no CDN) |
| Desktop shell | pywebview |
| Bank data | SimpleFIN protocol |
| Packaging | PyInstaller |

## License

MIT — see [LICENSE](LICENSE).
