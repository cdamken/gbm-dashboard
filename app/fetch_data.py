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
from datetime import date, datetime, timedelta, timezone
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
    from gbm_mx_api.errors import TransportError
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
    # Only try the saved session when we're NOT explicitly completing MFA.
    # With a --totp code in hand the user is finishing a fresh login, so go
    # straight to it: calling from_saved() first would attempt a doomed
    # refresh_session() network round-trip on the dead session (the reason
    # we're here), and that latency can push complete_mfa() past the code's
    # 30-second TOTP window → a spurious "invalid or expired TOTP code".
    if totp_code is None:
        client = GbmClient.from_saved()
        if client is not None:
            return client
        if non_interactive:
            # The dashboard's first POST /update with no TOTP — tell the
            # browser to show its MFA modal.
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
# Incremental fetch helpers
# ---------------------------------------------------------------------------
# Buffer in days for the incremental window: we re-fetch this many days
# BEFORE the last_update timestamp so late settlements (T+2 trades,
# dividends that the server posts a few days after the ex-date, etc.)
# get picked up and merged. The merge dedupes by unique id, so the
# overlap is harmless.
INCREMENTAL_BUFFER_DAYS = 14


def read_last_update_date(data_dir: Path) -> date | None:
    """Return the date portion of DATA/last_update.date, or None.

    None triggers full-window fetch in main(). The file is written at
    the end of every successful run, so its absence means either first
    run or a wipe via /reset.
    """
    path = data_dir / "last_update.date"
    if not path.exists():
        return None
    try:
        first_line = path.read_text(encoding="utf-8").strip().splitlines()[0]
        # Accept legacy "2026-06-10 09:21:43" AND new ISO
        # "2026-06-10T09:21:43Z" — only the YYYY-MM-DD prefix matters
        # for the incremental window calculation.
        date_part = first_line.split('T')[0].split()[0]
        return date.fromisoformat(date_part)
    except (OSError, ValueError, IndexError):
        return None


def merge_records(
    existing_path: Path,
    new_payload: dict,
    list_field: str,
    key_fn,
    sort_key: str,
    sort_reverse: bool = True,
) -> dict:
    """Merge ``new_payload[list_field]`` into the existing JSON at path.

    On key collision, the NEW record wins (so server-side corrections
    propagate — e.g. a pending order flipping to filled). Existing records
    not present in the new fetch are kept intact (they're older than the
    incremental cutoff). Also preserves the older ``from_date`` so the
    JSON's metadata reflects the full window covered, not just this
    incremental slice.

    Mutates new_payload[list_field] in place and returns new_payload.
    """
    existing_records: list = []
    existing_from: str | None = None
    if existing_path.exists():
        try:
            with existing_path.open(encoding="utf-8") as f:
                existing = json.load(f)
            existing_records = existing.get(list_field, []) or []
            existing_from = existing.get("from_date")
        except (json.JSONDecodeError, OSError):
            pass  # treat as fresh fetch

    by_key: dict = {}
    # Existing first so new records take precedence on collision.
    for r in existing_records:
        try:
            by_key[key_fn(r)] = r
        except (KeyError, TypeError):
            continue
    for r in new_payload.get(list_field, []) or []:
        try:
            by_key[key_fn(r)] = r
        except (KeyError, TypeError):
            continue
    merged = list(by_key.values())
    merged.sort(key=lambda r: r.get(sort_key, "") or "", reverse=sort_reverse)
    new_payload[list_field] = merged

    new_from = new_payload.get("from_date")
    if existing_from and (not new_from or existing_from < new_from):
        new_payload["from_date"] = existing_from
    return new_payload


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
            "scratch. Without this flag the script reads DATA/last_update.date "
            "and only fetches transactions since (last_update - "
            f"{INCREMENTAL_BUFFER_DAYS} days), merging the results into the "
            "existing JSON files by unique id."
        ),
    )
    args = parser.parse_args()

    last_update = read_last_update_date(DATA_DIR)
    incremental = last_update is not None and not args.full
    if incremental:
        # Pull this many days before the last_update timestamp to catch
        # late settlements; merge will dedupe.
        incremental_from = last_update - timedelta(days=INCREMENTAL_BUFFER_DAYS)
        print(
            f"Incremental mode — fetching since {incremental_from} "
            f"(last_update = {last_update}, buffer = {INCREMENTAL_BUFFER_DAYS}d)"
        )
    else:
        reason = (
            "forced via --full" if args.full
            else "first run / no last_update.date"
        )
        print(f"Full mode ({reason}) — pulling the configured days window.")

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

            # The v3/dashboard/investments-groups endpoint is what the
            # GBM mobile app uses to compute "TOTAL INVERTIDO". Its FX
            # rate matches the mobile app exactly, so we save it as the
            # authoritative source for the total value card.
            email = os.environ.get("GBM_EMAIL", "")
            if email:
                try:
                    ig = client.dashboard.investments_groups(contract.contract_id, email)
                    write_json(
                        DATA_DIR / "investments_groups.json",
                        ig.model_dump(by_alias=False),
                    )
                    print(
                        f"  investments-groups: total=${float(ig.total_position.amount):,.2f} "
                        f"({len(ig.groups)} groups)"
                    )
                except (ApiError, TransportError) as e:
                    # This endpoint times out frequently (it joins live FX,
                    # homebroker, and offshore data server-side). Treat a
                    # timeout as non-fatal — the dashboard will fall back
                    # to the per-account sum which is close enough.
                    print(f"  investments-groups: SKIPPED ({type(e).__name__}: {e})")
            else:
                print("  investments-groups: skipped (no GBM_EMAIL in env)")

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
            # Include Trading USA (template "trading_usa") alongside the
            # Mexican "trading" accounts — its orders come from the same
            # GetBlotterOrders endpoint. The "== 'trading'" filter silently
            # dropped Trading USA, so USA buys/sells (e.g. a partial sell)
            # never showed in Movimientos even though MX ones did. The
            # per-account call is wrapped in try/except, so if GBM rejects
            # the USA account it's logged and skipped rather than fatal.
            trading_accounts = [
                a for a in accounts
                if a.management_type_template in ("trading", "trading_usa")
            ]
            if trading_accounts:
                to_date_ = date.today()
                if incremental:
                    from_date_ = incremental_from
                else:
                    # Full backfill window. GetBlotterOrders is queried DAY BY
                    # DAY (the endpoint returns one day per call), so the
                    # window size directly drives how many sequential HTTP
                    # calls a full reload makes (× each trading account). The
                    # old 3650-day (10-year) default meant ~3,650 calls per
                    # account even on a months-old account — thousands of
                    # them empty — which blew past the ownCloud subprocess
                    # timeout and got SIGKILL'd mid-fetch. 365 days covers a
                    # young account with margin; bump GBM_ORDERS_DAYS for an
                    # older one.
                    days_back = int(os.environ.get("GBM_ORDERS_DAYS", "365"))
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
                filled_payload = {
                    "from_date": from_date_.isoformat(),
                    "to_date": to_date_.isoformat(),
                    "accounts": accounts_meta,
                    "orders": filled_orders,
                }
                # All statuses (Histórico page).
                all_payload = {
                    "from_date": from_date_.isoformat(),
                    "to_date": to_date_.isoformat(),
                    "accounts": accounts_meta,
                    "orders": all_orders,
                }
                if incremental:
                    # Merge by sob_id (9-digit unique order id). A pending
                    # order from a previous run can flip to filled in this
                    # fetch — the new record wins on collision.
                    filled_payload = merge_records(
                        DATA_DIR / "orders.json", filled_payload,
                        list_field="orders",
                        key_fn=lambda r: r.get("sob_id"),
                        sort_key="processed_at",
                    )
                    all_payload = merge_records(
                        DATA_DIR / "orders_all.json", all_payload,
                        list_field="orders",
                        key_fn=lambda r: r.get("sob_id"),
                        sort_key="processed_at",
                    )
                write_json(DATA_DIR / "orders.json", filled_payload)
                write_json(DATA_DIR / "orders_all.json", all_payload)
            else:
                print("  no trading accounts → skipping orders download.")

            # ----------------------------------------------------------
            # Dividends (cash distributions). Lives on api.appgbm.com,
            # paginates server-side. We iterate every trading account so
            # users with multiple contracts see them all.
            # ----------------------------------------------------------
            if trading_accounts:
                div_to = date.today()
                if incremental:
                    div_from = incremental_from
                else:
                    div_days_back = int(os.environ.get("GBM_DIVIDENDS_DAYS", "3650"))
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
                div_file_payload = {
                    "from_date": div_from.isoformat(),
                    "to_date": div_to.isoformat(),
                    "dividends": dividends_payload,
                }
                if incremental:
                    div_file_payload = merge_records(
                        DATA_DIR / "dividends.json", div_file_payload,
                        list_field="dividends",
                        key_fn=lambda r: r.get("transaction_id"),
                        sort_key="process_date",
                    )
                write_json(DATA_DIR / "dividends.json", div_file_payload)

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
                tx_to = date.today()
                if incremental:
                    tx_from = incremental_from
                else:
                    tx_days_back = int(os.environ.get("GBM_TRANSACTIONS_DAYS", "3650"))
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
                tx_file_payload = {
                    "from_date": tx_from.isoformat(),
                    "to_date": tx_to.isoformat(),
                    "accounts": accounts_meta_all,
                    "transactions": transactions_payload,
                }
                if incremental:
                    tx_file_payload = merge_records(
                        DATA_DIR / "transactions.json", tx_file_payload,
                        list_field="transactions",
                        key_fn=lambda r: r.get("transaction_id"),
                        sort_key="process_date",
                    )
                write_json(DATA_DIR / "transactions.json", tx_file_payload)

            # ISO 8601 UTC with explicit Z — browser JS parses the `Z`
            # and converts to user-local via toLocaleTimeString(). Fixes
            # the "Updated 07:21 AM" stale chip on a UTC server.
            (DATA_DIR / "last_update.date").write_text(
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ\n"),
                encoding="utf-8",
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
