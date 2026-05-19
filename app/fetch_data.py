#!/usr/bin/env python3
"""Fetch live portfolio data from GBM+ via gbm-mx-api and write JSON to DATA/.

Two modes:

  - No args: interactive. If session expired, asks TOTP via stdin.
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
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
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
    from gbm_mx_api import ApiError, AuthError, GbmClient, MfaRequired
except ImportError:
    sys.stderr.write(
        "gbm-mx-api is not installed. Run from the project root: ./dashboard.sh update\n"
    )
    sys.exit(30)


# ---------------------------------------------------------------------------
# Login helpers
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
      1. If ~/.gbm-mx/session.json is valid -> reuse it (no TOTP needed).
      2. Else if --totp was passed -> use it.
      3. Else if interactive -> prompt for TOTP.
      4. Else -> exit 10 (browser needs MFA modal).
    """
    client = GbmClient.from_saved()
    if client is not None:
        return client

    if non_interactive and totp_code is None:
        # The dashboard's first POST /update with no TOTP — tell the browser
        # to show its MFA modal.
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
        return GbmClient.login(email=email, password=password, totp_provider=totp_provider)
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
# JSON helpers
# ---------------------------------------------------------------------------
def to_jsonable(obj):
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def write_json(path: Path, data) -> None:
    payload = to_jsonable(data)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    size_kb = path.stat().st_size / 1024
    print(f"  wrote {path.relative_to(PROJECT_DIR)} ({size_kb:.1f} KB)")


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
    args = parser.parse_args()

    print("Connecting to GBM+...")
    try:
        with get_client(args.totp, args.non_interactive) as client:
            contract = client.contracts.get_main()
            print(f"  contract: {contract.legacy_contract_id}")

            accounts = client.accounts.list(contract.contract_id)
            print(f"  accounts: {len(accounts)}")

            accounts_payload = [
                {
                    "legacy_contract_id": a.legacy_contract_id,
                    "account_id": a.account_id,
                    "name": a.name,
                    "number": a.number,
                    "management_type_template": a.management_type_template,
                    "position": {
                        "amount": float(a.position.amount) if a.position else None,
                        "currency": a.position.currency if a.position else None,
                    },
                    "plus_minus": {
                        "amount": float(a.plus_minus.amount) if a.plus_minus else None,
                        "currency": a.plus_minus.currency if a.plus_minus else None,
                    },
                    "plus_minus_percentage": a.plus_minus_percentage,
                    "status": a.status,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in accounts
            ]
            write_json(DATA_DIR / "accounts.json", accounts_payload)

            # All 5 investment sections used by GBM's GetPositionSummary.
            INVEST_SECTIONS = (
                "mercados_globales_sic",
                "mercado_capitales",
                "sociedades_inversion_deuda",
                "sociedades_inversion_comun",
                "mercado_extranjero",
            )
            positions_by_account: dict[str, object] = {}
            for a in accounts:
                try:
                    # Pass account_id (UUID) so non-primary accounts (Asesor,
                    # Trading USA) also return their full holdings.
                    summary = client.positions.summary(
                        a.legacy_contract_id, account_id=a.account_id
                    )
                    positions_by_account[a.legacy_contract_id] = to_jsonable(
                        summary.model_dump(by_alias=False)
                    )
                    count = sum(
                        1
                        for section_key in INVEST_SECTIONS
                        for p in positions_by_account[a.legacy_contract_id].get(section_key)
                        or []
                        if p.get("issue_id") != "Subtotal"
                    )
                    print(f"  positions for {a.legacy_contract_id} ({a.name}): {count}")
                except ApiError as e:
                    print(f"  positions for {a.legacy_contract_id} ({a.name}): {e}")
                    positions_by_account[a.legacy_contract_id] = None

            write_json(DATA_DIR / "positions.json", positions_by_account)

            (DATA_DIR / "last_update.date").write_text(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S\n"), encoding="utf-8"
            )

    except ApiError as e:
        sys.stderr.write(f"API error: {e}\n")
        sys.exit(20)

    print("OK Fetch complete.")


if __name__ == "__main__":
    main()
