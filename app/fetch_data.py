#!/usr/bin/env python3
"""Fetch live GBM+ portfolio data and write JSON to DATA/.

Thin host adapter around ``gbm_mx_api.sync`` — the shared data núcleo. This
script only does what's specific to the *local single-user* host: read creds
from ``app/.env``, prompt for a TOTP on stdin when interactive, and map auth
failures to process exit codes the HTTP server understands. The actual
fetch+write pipeline lives in the library (see gbm-mx-api/src/gbm_mx_api/sync.py
and ADR ``2026-06-16 — ALL — Núcleo compartido``).

Two modes:

  - No args: interactive. If the session expired, asks TOTP via stdin.
  - --totp CODE: non-interactive. Used by the dashboard's Update button —
    the browser supplies the 6-digit code via a modal.

Exit codes (so the HTTP server can map them to HTTP statuses):
  0   success
  10  session expired AND no TOTP provided (browser needs to show MFA modal)
  11  TOTP code invalid (challenge failed)
  12  credentials invalid (wrong email/password)
  20  network / API error
  30  configuration error (.env missing, lib not installed, etc.)
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_DIR / "app"
DATA_DIR = PROJECT_DIR / "DATA"
DATA_DIR.mkdir(exist_ok=True)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv(APP_DIR / ".env")


try:
    from gbm_mx_api import (
        ApiError,
        AuthError,
        GbmClient,
        MfaRequired,
        sync,
        try_refresh_saved,
    )
except ImportError:
    sys.stderr.write(
        "gbm-mx-api is not installed. Run from the project root: ./dashboard.sh update\n"
    )
    sys.exit(30)


# ---------------------------------------------------------------------------
# Login helpers (host-specific: stdin vs. fixed TOTP)
# ---------------------------------------------------------------------------
def make_interactive_totp_provider():
    def _provider() -> str:
        code = input("TOTP code (6 digits): ").strip()
        if not (code.isdigit() and len(code) == 6):
            sys.stderr.write("ERROR: TOTP must be exactly 6 digits.\n")
            sys.exit(11)
        return code

    return _provider


def make_fixed_totp_provider(code: str):
    if not (code.isdigit() and len(code) == 6):
        sys.stderr.write("ERROR: --totp must be exactly 6 digits.\n")
        sys.exit(11)

    def _provider() -> str:
        return code

    return _provider


def get_client(totp_code: str | None, non_interactive: bool) -> GbmClient:
    """Return a usable GbmClient or exit with a specific code.

    Resolution:
      1. No --totp: try the saved session (proactive refresh in the lib). If
         it yields a client, reuse it. Else, in non-interactive mode exit 10
         so the browser shows its MFA modal.
      2. --totp / interactive: log in fresh with the supplied or prompted code.
    """
    if totp_code is None:
        client = try_refresh_saved()  # default ~/.gbm-mx/session.json
        if client is not None:
            return client
        if non_interactive:
            # First POST /update with no TOTP — tell the browser to show its
            # MFA modal.
            sys.stderr.write("MFA_REQUIRED: session expired, TOTP needed.\n")
            sys.exit(10)

    email = os.environ.get("GBM_EMAIL")
    password = os.environ.get("GBM_PASSWORD")
    if non_interactive and not (email and password):
        sys.stderr.write("ERROR: GBM_EMAIL / GBM_PASSWORD missing from .env.\n")
        sys.exit(30)
    if not email:
        email = input("GBM email: ").strip()
    if not password:
        password = getpass.getpass("GBM password: ")

    totp_provider = (
        make_fixed_totp_provider(totp_code)
        if totp_code
        else make_interactive_totp_provider()
    )

    try:
        return GbmClient.login(
            email=email, password=password, totp_provider=totp_provider
        )
    except AuthError as e:
        msg = str(e).lower()
        # AWS Cognito returns specific error messages for bad TOTP vs bad creds.
        if "code" in msg or "mfa" in msg or "challenge" in msg or "totp" in msg:
            sys.stderr.write("ERROR: invalid or expired TOTP code.\n")
            sys.exit(11)
        sys.stderr.write(f"ERROR: bad credentials ({e}).\n")
        sys.exit(12)
    except MfaRequired as e:
        # Shouldn't bubble here (login() handles it), but just in case:
        sys.stderr.write(f"ERROR: MFA challenge unresolved ({e}).\n")
        sys.exit(11)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch GBM data → DATA/*.json")
    parser.add_argument(
        "--totp",
        help="6-digit TOTP code (used by the dashboard's Update button).",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Don't prompt for anything. Used by the HTTP server.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Force a full-window fetch (skip incremental). Use this on first "
            "run, after /reset, or when you want to re-pull stale data from "
            "scratch. Without this flag the lib reads DATA/last_update.date and "
            "only fetches since (last_update − buffer), merging by unique id."
        ),
    )
    args = parser.parse_args()

    print("Connecting to GBM+...")
    try:
        with get_client(args.totp, args.non_interactive) as client:
            sync(
                client,
                DATA_DIR,
                full=args.full,
                email=os.environ.get("GBM_EMAIL"),
                secure=False,
            )
    except AuthError as e:
        # The saved session was rejected (token revoked or expired earlier than
        # expected). Wipe it and tell the caller MFA is required so the browser
        # opens the TOTP modal automatically.
        session_path = Path.home() / ".gbm-mx" / "session.json"
        try:
            session_path.unlink(missing_ok=True)
        except OSError:
            pass
        sys.stderr.write(f"Saved session rejected by GBM ({e}). Removed.\n")
        sys.exit(10)  # mfa_required
    except ApiError as e:
        sys.stderr.write(f"API error: {e}\n")
        sys.exit(20)

    print("OK Fetch complete.")


if __name__ == "__main__":
    main()
