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
from datetime import date, datetime, timedelta
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
    """Atomically write JSON: tmp → fsync → rename.

    Without this, killing the server mid-write (Ctrl-C, kill, OOM) leaves
    a truncated JSON file and the dashboard shows "Sin datos" until the
    next successful update.
    """
    payload = to_jsonable(data)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
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

            # list_with_dashboard merges the legacy /v2 endpoint (balances)
            # with the newer appgbm.com /dashboard endpoint (which includes
            # the otherwise-hidden Smart Cash Dólares account).
            accounts = client.accounts.list_with_dashboard(contract.contract_id)
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

            # ----------------------------------------------------------
            # Orders (filled) for EVERY trading account (BMV). A user may
            # have multiple trading accounts (e.g. Personal, Asesor, family
            # members under the same login) and orders live in different
            # places. We iterate them all and tag each order with its
            # account so the dashboard can filter / group.
            # ----------------------------------------------------------
            trading_accounts = [
                a for a in accounts if a.management_type_template == "trading"
            ]
            if trading_accounts:
                days_back = int(os.environ.get("GBM_ORDERS_DAYS", "90"))
                to_date_ = date.today()
                from_date_ = to_date_ - timedelta(days=days_back)
                print(
                    f"  fetching orders {from_date_} → {to_date_} "
                    f"for {len(trading_accounts)} trading account(s)..."
                )

                # We download ALL orders (any status) once per account
                # using list_for_range, then derive the filled-only view
                # for backward compatibility with the Movimientos page.
                # This avoids hitting the backend twice per day.
                all_orders: list[dict] = []
                filled_orders: list[dict] = []
                for acct in trading_accounts:
                    try:
                        raw_orders = client.orders.list_for_range(
                            acct.legacy_contract_id, from_date_, to_date_
                        )
                    except ApiError as e:
                        print(f"  {acct.name} ({acct.legacy_contract_id}): {e}")
                        continue
                    n_filled = sum(1 for o in raw_orders if o.is_filled)
                    print(
                        f"  {acct.name} ({acct.legacy_contract_id}): "
                        f"{len(raw_orders)} total, {n_filled} filled"
                    )
                    for o in raw_orders:
                        # Common shape for both files.
                        amount = float(o.assigned_quantity * o.average_price)
                        common = {
                            "sob_id": o.sob_id,
                            "account_id": o.account_id,
                            "issue_id": o.issue_id,
                            "instrument_type": int(o.instrument_type),
                            "side": o.side.name,
                            "status": o.status,
                            "status_label": o.status_label,
                            "is_filled": o.is_filled,
                            "is_cancelled": o.is_cancelled,
                            "original_quantity": o.original_quantity,
                            "assigned_quantity": o.assigned_quantity,
                            "cancel_quantity": o.cancel_quantity,
                            "quantity": o.assigned_quantity if o.is_filled
                                        else o.original_quantity,
                            "average_price": float(o.average_price),
                            "limit_price": float(o.price),
                            "amount": amount,
                            "commission": float(o.commission),
                            "iva": float(o.iva),
                            "processed_at": o.process_date.isoformat(),
                            "cancel_message": o.cancel_message,
                            "account_legacy_id": acct.legacy_contract_id,
                            "account_name": acct.name,
                        }
                        all_orders.append(common)
                        if o.is_filled:
                            filled_orders.append(common)

                # Chronological for stable display.
                all_orders.sort(key=lambda o: o["processed_at"])
                filled_orders.sort(key=lambda o: o["processed_at"])

                accounts_meta = [
                    {
                        "legacy_contract_id": a.legacy_contract_id,
                        "name": a.name,
                    }
                    for a in trading_accounts
                ]
                # Filled-only (Movimientos page) for backward compat.
                write_json(
                    DATA_DIR / "orders.json",
                    {
                        "from_date": from_date_.isoformat(),
                        "to_date": to_date_.isoformat(),
                        "accounts": accounts_meta,
                        "orders": filled_orders,
                    },
                )
                # All statuses (Histórico page).
                write_json(
                    DATA_DIR / "orders_all.json",
                    {
                        "from_date": from_date_.isoformat(),
                        "to_date": to_date_.isoformat(),
                        "accounts": accounts_meta,
                        "orders": all_orders,
                    },
                )
            else:
                print("  no trading accounts → skipping orders download.")

            # ----------------------------------------------------------
            # Dividends (cash distributions). Lives on api.appgbm.com,
            # paginates server-side. We iterate every trading account so
            # users with multiple contracts see them all.
            # ----------------------------------------------------------
            if trading_accounts:
                div_days_back = int(os.environ.get("GBM_DIVIDENDS_DAYS", "365"))
                div_to = date.today()
                div_from = div_to - timedelta(days=div_days_back)
                print(
                    f"  fetching dividends {div_from} → {div_to} "
                    f"for {len(trading_accounts)} trading account(s)..."
                )
                dividends_payload: list[dict] = []
                for acct in trading_accounts:
                    try:
                        divs = client.dividends.list_for_range(
                            contract.contract_id,
                            acct.legacy_contract_id,
                            div_from,
                            div_to,
                        )
                    except ApiError as e:
                        # api.appgbm.com may reject our token (different
                        # Cognito client) — log and skip rather than fail
                        # the whole run.
                        print(
                            f"  dividends {acct.name} ({acct.legacy_contract_id}): {e}"
                        )
                        continue
                    print(
                        f"  dividends {acct.name} ({acct.legacy_contract_id}): "
                        f"{len(divs)} item(s)"
                    )
                    for d in divs:
                        dividends_payload.append(
                            {
                                "transaction_id": d.transaction_id,
                                "security_id": d.security_id,
                                "security_name": d.security_name,
                                "description": d.transaction_description,
                                "amount": float(d.transaction_amount),
                                "net_amount": float(d.transaction_net_amount),
                                "is_withholding": d.is_withholding,
                                "process_date": d.process_date.isoformat(),
                                "settlement_date": (
                                    d.settlement_date.isoformat()
                                    if d.settlement_date
                                    else None
                                ),
                                "transaction_time": d.transaction_time,
                                "account_legacy_id": acct.legacy_contract_id,
                                "account_name": acct.name,
                            }
                        )
                dividends_payload.sort(key=lambda d: d["process_date"], reverse=True)
                write_json(
                    DATA_DIR / "dividends.json",
                    {
                        "from_date": div_from.isoformat(),
                        "to_date": div_to.isoformat(),
                        "dividends": dividends_payload,
                    },
                )

            # ----------------------------------------------------------
            # Transactions (full ledger). Lives on api.appgbm.com — same
            # endpoint as dividends but with no transac_type filter so we
            # get EVERY movement: stock buys/sells, fund buys/sells,
            # repos, cash transfers, FX, dividends. Iterated over ALL
            # accounts (not just trading) so Smart Cash, Asesor and
            # Trading USA are covered. Movements are tagged with the
            # account so the dashboard can filter.
            # ----------------------------------------------------------
            if accounts:
                tx_days_back = int(os.environ.get("GBM_TRANSACTIONS_DAYS", "365"))
                tx_to = date.today()
                tx_from = tx_to - timedelta(days=tx_days_back)
                print(
                    f"  fetching transactions {tx_from} → {tx_to} "
                    f"for {len(accounts)} account(s)..."
                )
                transactions_payload: list[dict] = []
                for acct in accounts:
                    try:
                        txs = client.transactions.list_for_range(
                            contract.contract_id,
                            acct.legacy_contract_id,
                            tx_from,
                            tx_to,
                        )
                    except ApiError as e:
                        print(
                            f"  transactions {acct.name} "
                            f"({acct.legacy_contract_id}): {e}"
                        )
                        continue
                    print(
                        f"  transactions {acct.name} ({acct.legacy_contract_id}): "
                        f"{len(txs)} item(s)"
                    )
                    for t in txs:
                        transactions_payload.append(
                            {
                                "transaction_id": t.transaction_id,
                                "security_id": t.security_id,
                                "security_name": t.security_name,
                                "transaction_type": t.transaction_type,
                                "sub_transaction_type": t.sub_transaction_type,
                                "description": t.transaction_description,
                                "category": t.category,
                                "is_buy": t.is_buy,
                                "is_sell": t.is_sell,
                                "is_cash_flow": t.is_cash_flow,
                                "amount": float(t.transaction_amount),
                                "net_amount": float(t.transaction_net_amount),
                                "quantity": float(t.quantity),
                                "price": float(t.transaction_price),
                                "commission": float(t.transaction_commission),
                                "tax": float(t.transaction_tax),
                                "process_date": t.process_date.isoformat(),
                                "settlement_date": (
                                    t.settlement_date.isoformat()
                                    if t.settlement_date
                                    else None
                                ),
                                "transaction_time": t.transaction_time,
                                "account_legacy_id": acct.legacy_contract_id,
                                "account_name": acct.name,
                            }
                        )
                transactions_payload.sort(
                    key=lambda t: t["process_date"], reverse=True
                )
                accounts_meta_all = [
                    {"legacy_contract_id": a.legacy_contract_id, "name": a.name}
                    for a in accounts
                ]
                write_json(
                    DATA_DIR / "transactions.json",
                    {
                        "from_date": tx_from.isoformat(),
                        "to_date": tx_to.isoformat(),
                        "accounts": accounts_meta_all,
                        "transactions": transactions_payload,
                    },
                )

            (DATA_DIR / "last_update.date").write_text(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S\n"), encoding="utf-8"
            )

    except AuthError as e:
        # The saved session was rejected (token revoked or expired earlier
        # than expected). Wipe it and tell the caller MFA is required so the
        # browser opens the TOTP modal automatically.
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
