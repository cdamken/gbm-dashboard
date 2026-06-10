"""Local HTTP server for the GBM Dashboard.

Endpoints:

  GET  /app/*.html        — dashboard HTML
  GET  /DATA/*.json       — data fetched by fetch_data.py
  GET  /config            — { "configured": bool, "email": str | null }
  POST /config            — body: { "email": str, "password": str }
                            saves credentials to app/.env (0600)
  POST /update            — request a refresh. Body: {} or {"totp_code": "..."}
                            Returns one of:
                              200 {"status": "ok", "output": "..."}
                              401 {"status": "mfa_required"}
                              401 {"status": "mfa_invalid"}
                              401 {"status": "auth_failed"}
                              500 {"status": "config_error"} | "error"
"""

import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

PORT = 8086
# Bind to loopback only — the dashboard is for the local user. Listening on
# 0.0.0.0 would expose /config (writes .env) and /update (runs subprocess)
# to anyone on the same Wi-Fi.
BIND_HOST = "127.0.0.1"
PROJECT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_DIR / "app"
ENV_FILE = APP_DIR / ".env"
VENV_PY = APP_DIR / ".venv" / "bin" / "python"
FETCH_SCRIPT = APP_DIR / "fetch_data.py"
DATA_DIR = PROJECT_DIR / "DATA"
PROGRESS_FILE = DATA_DIR / "update_progress.log"
PROGRESS_LOCK = threading.Lock()
# Tracks whether an update is currently running so /progress can tell.
UPDATE_STATE: dict[str, object] = {"running": False, "started_at": None}

# CSRF defense: writes (POST /config, POST /update) must come from a page
# served by this server — i.e. Origin == http://127.0.0.1:8086 or
# http://localhost:8086. Anything else (a malicious page in another tab)
# is rejected.
_ALLOWED_ORIGINS = frozenset(
    {
        f"http://127.0.0.1:{PORT}",
        f"http://localhost:{PORT}",
    }
)

# Map fetch_data.py exit codes to (HTTP status, JSON status string).
EXIT_CODE_MAP = {
    0: (200, "ok"),
    10: (401, "mfa_required"),
    11: (401, "mfa_invalid"),
    12: (401, "auth_failed"),
    20: (502, "api_error"),
    30: (500, "config_error"),
}

# Placeholder values shipped in .env.example — treat them as "unset".
PLACEHOLDER_EMAILS = {"tu-email@dominio.com", "tu-email@ejemplo.com"}
PLACEHOLDER_PASSWORDS = {"tu-password", "tu-password-aqui", ""}


def _parse_env(path: Path) -> dict[str, str]:
    """Read a .env file into a dict. Returns empty dict if file is missing."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _is_configured(env: dict[str, str]) -> bool:
    email = env.get("GBM_EMAIL", "").strip()
    password = env.get("GBM_PASSWORD", "")
    if not email or email in PLACEHOLDER_EMAILS:
        return False
    if not password or password in PLACEHOLDER_PASSWORDS:
        return False
    return True


def _write_env_keys(updates: dict[str, str]) -> None:
    """Merge ``updates`` into ``app/.env``, writing atomically with 0600 perms.

    Preserves any other keys that were in .env so the user can still
    tweak them by hand if needed.
    """
    merged = _parse_env(ENV_FILE)
    merged.update(updates)

    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ENV_FILE.with_suffix(".env.tmp")
    lines = [
        "# Managed by the dashboard's config UI. .env is gitignored.",
        "# Edit by hand if you prefer.",
        "",
    ]
    for k, v in merged.items():
        # Wrap in single quotes so special chars ($, =, spaces) stay literal.
        # The parser strips outer quotes on read.
        lines.append(f"{k}='{v}'")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_FILE)


def _write_env(email: str, password: str) -> None:
    """Convenience: persist credentials only."""
    _write_env_keys({"GBM_EMAIL": email, "GBM_PASSWORD": password})


# Days-back env keys controllable from the Settings page. Defaults
# match the fallbacks in fetch_data.py.
#
# Defaults: 10 years (the validated maximum, 1..3650). GBM's API
# doesn't impose a hard ceiling on date range — we paginate. Old
# defaults (90/365/365) made XIRR look broken on accounts older than
# one year because the metric couldn't reconcile flows it never saw.
# Users can lower these in Configuración for faster updates.
_DAYS_KEYS = {
    "orders_days":       ("GBM_ORDERS_DAYS",      3650),
    "dividends_days":    ("GBM_DIVIDENDS_DAYS",   3650),
    "transactions_days": ("GBM_TRANSACTIONS_DAYS", 3650),
}


def _wipe_session_and_data() -> None:
    """Drop the cached session token and any DATA files.

    Called when the user switches to a different account in the config UI.
    Prevents the new account from inheriting the previous account's session
    or seeing stale holdings/orders.
    """
    DATA_DIR = PROJECT_DIR / "DATA"
    session_path = Path.home() / ".gbm-mx" / "session.json"
    try:
        session_path.unlink(missing_ok=True)
    except OSError:
        pass
    for fname in (
        "accounts.json",
        "positions.json",
        "orders.json",
        "last_update.date",
    ):
        try:
            (DATA_DIR / fname).unlink(missing_ok=True)
        except OSError:
            pass


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    def log_message(self, format, *args):
        # Reduce noise — only log non-static requests.
        path = getattr(self, "path", "")
        if any(p in path for p in ("/update", "/config")) or "code" in format:
            super().log_message(format, *args)

    def end_headers(self):
        # Force the browser to revalidate every request. Without this, a stale
        # cached HTML/JSON makes the dashboard show old timestamps even after
        # the user clicks Update and the server actually refetched.
        path = getattr(self, "path", "")
        if path.startswith("/app/") or path.startswith("/DATA/"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------
    def do_GET(self):
        # Send the bare host straight to the dashboard so users don't
        # have to remember /app/index.html. Also blocks the default
        # directory listing for any other folder (/.git/, /DATA/, etc.)
        # — on a loopback-only server it's not a security hole, just
        # noise that exposes filesystem structure.
        # Includes `/app` and `/app/` so the user can also type just
        # `localhost:8086/app` and land on the dashboard (otherwise the
        # stdlib http.server 301s to `/app/`, which then hits the
        # directory-listing block below).
        if self.path in ("", "/", "/app", "/app/"):
            self.send_response(302)
            self.send_header("Location", "/app/index.html")
            self.end_headers()
            return
        if self.path.endswith("/"):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Directory listing disabled. Go to /app/index.html\n")
            return
        if self.path == "/config":
            env = _parse_env(ENV_FILE)
            configured = _is_configured(env)
            email = env.get("GBM_EMAIL", "") if configured else None
            self._json(200, {"configured": configured, "email": email})
            return
        if self.path == "/settings":
            env = _parse_env(ENV_FILE)
            payload: dict[str, object] = {}
            for js_key, (env_key, default) in _DAYS_KEYS.items():
                raw = env.get(env_key, "").strip()
                try:
                    payload[js_key] = int(raw) if raw else default
                except ValueError:
                    payload[js_key] = default
            payload["dashboard_version"] = "0.13.0"
            try:
                from gbm_mx_api import __version__ as _api_version
                payload["gbm_mx_api_version"] = _api_version
            except Exception:
                payload["gbm_mx_api_version"] = "unknown"
            self._json(200, payload)
            return
        if self.path == "/export/transactions.csv":
            self._handle_export_transactions_csv()
            return
        # Per-page CSV exports — focused subsets matching each dashboard
        # page's columns. Complement to the SAT-shaped 13-column dump.
        if self.path == "/export/ordenes.csv":
            return self._handle_export_page_csv("ordenes")
        if self.path == "/export/historico.csv":
            return self._handle_export_page_csv("historico")
        if self.path == "/export/dividendos.csv":
            return self._handle_export_page_csv("dividendos")
        if self.path == "/export/transacciones.csv":
            return self._handle_export_page_csv("transacciones")
        if self.path == "/export/posiciones.csv":
            return self._handle_export_page_csv("posiciones")
        if self.path.startswith("/benchmark/"):
            # /benchmark/{symbol} — proxy to Yahoo Finance with 24h cache.
            # The client URL-encodes special chars (`^` → `%5E`) so we must
            # decode here before the regex validator sees the value. Otherwise
            # `^SP500TR` arrives as the literal string `%5ESP500TR` and gets
            # rejected as `invalid symbol` (the regex doesn't allow `%`).
            raw = self.path[len("/benchmark/"):].split("?", 1)[0]
            symbol = urllib.parse.unquote(raw)
            self._handle_benchmark(symbol)
            return
        if self.path == "/progress":
            text = ""
            if PROGRESS_FILE.exists():
                try:
                    text = PROGRESS_FILE.read_text(encoding="utf-8")[-4000:]
                except OSError:
                    text = ""
            elapsed = None
            with PROGRESS_LOCK:
                running = bool(UPDATE_STATE["running"])
                started = UPDATE_STATE["started_at"]
            if running and started:
                elapsed = round(time.time() - float(started), 1)
            self._json(200, {"running": running, "elapsed_s": elapsed, "log": text})
            return
        super().do_GET()

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    def do_POST(self):
        # CSRF defense. Browsers always send Origin on POST; if a request
        # arrives without one OR with a foreign origin, refuse.
        origin = self.headers.get("Origin", "")
        if origin and origin not in _ALLOWED_ORIGINS:
            self._json(403, {"status": "forbidden", "detail": "bad origin"})
            return

        if self.path == "/config":
            self._handle_config()
            return
        if self.path == "/settings":
            self._handle_settings()
            return
        if self.path == "/reset":
            self._handle_reset()
            return
        if self.path == "/update":
            self._handle_update()
            return
        self.send_response(404)
        self.end_headers()

    def _handle_reset(self):
        """Revoke the session and wipe DATA.

        Two layers of cleanup:
          1. Try to call Cognito GlobalSignOut so the refresh_token is
             invalidated server-side — this is the difference between
             "deleted my local file" and "really logged out". Best-effort:
             a network failure or expired access token doesn't block the
             local wipe.
          2. Delete ~/.gbm-mx/session.json + every DATA/*.json. Credentials
             in app/.env are kept — the user explicitly asked to revoke
             the session, not to forget the account.
        """
        signed_out = False
        signout_detail: str | None = None
        try:
            # Lazy import keeps server startup snappy (avoids pulling
            # httpx + pydantic at boot when /reset is never hit).
            from gbm_mx_api.auth.refresh import global_signout, refresh_session
            from gbm_mx_api.auth.session import Session

            session = Session.try_load()
            if session is not None:
                # GlobalSignOut requires a NON-expired access token. If
                # ours expired, refresh it first using the refresh_token
                # we're about to invalidate — small detour, cleaner result.
                if session.is_expired and session.refresh_token:
                    try:
                        session = refresh_session(session)
                    except Exception as exc:
                        signout_detail = f"refresh before signout failed: {exc}"
                        session = None
                if session is not None:
                    global_signout(session)
                    signed_out = True
        except Exception as exc:
            # Cognito unreachable, access token already revoked, etc.
            # Don't fail the /reset — local wipe is still useful.
            signout_detail = f"{type(exc).__name__}: {exc}"

        _wipe_session_and_data()
        self._json(
            200,
            {
                "status": "ok",
                "signed_out_globally": signed_out,
                "signout_detail": signout_detail,
            },
        )

    def _handle_benchmark(self, symbol: str):
        """Fetch Yahoo Finance monthly closes for ``symbol`` (cached 24h).

        Browser fetches CAN'T hit Yahoo directly (CORS blocks the
        public chart endpoint), so we proxy + cache server-side. The
        cache lives at DATA/benchmark_cache/{symbol}.json. Cache hits
        are < 5 ms; cache misses go out to Yahoo (~300-800 ms).

        Accepts symbols like ``NAFTRACISHRS.MX`` (NAFTRAC BMV), ``SPY``
        (S&P 500 ETF). Allowed characters: ``[A-Za-z0-9.^-]``. Any
        other request is refused — this is a defensive measure since
        the symbol is a path segment.
        """
        import re
        import urllib.error
        import urllib.request
        from datetime import datetime, timedelta

        if not re.fullmatch(r"[A-Za-z0-9.^_-]{1,40}", symbol):
            self._json(400, {"status": "bad_request", "detail": "invalid symbol"})
            return

        cache_dir = DATA_DIR / "benchmark_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{symbol}.json"

        # Cache hit?
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                fetched_at = datetime.fromisoformat(cached.get("fetched_at", "1970-01-01T00:00:00"))
                if (datetime.now() - fetched_at).total_seconds() < 24 * 3600:
                    self._json(200, cached)
                    return
            except (json.JSONDecodeError, ValueError, KeyError, OSError):
                pass  # corrupt cache → re-fetch

        # Default window: last 5 years (good enough for any GBM user).
        end_date = datetime.now()
        start_date = end_date - timedelta(days=365 * 5)
        p1 = int(start_date.timestamp())
        p2 = int(end_date.timestamp())
        # interval=1d so the benchmark replay line moves day-by-day,
        # not in monthly stair-steps. Yahoo's daily endpoint returns
        # ~252 closes/year (trading days) — much smoother chart.
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?period1={p1}&period2={p2}&interval=1d&events=history"
        )

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
            self._json(
                502,
                {
                    "status": "fetch_failed",
                    "detail": f"yahoo fetch error: {type(e).__name__}: {e}",
                    "symbol": symbol,
                },
            )
            return

        result = (payload.get("chart") or {}).get("result") or [{}]
        result = result[0] if result else {}
        timestamps = result.get("timestamp") or []
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        history = []
        for t, c in zip(timestamps, closes):
            if c is None:
                continue
            d = datetime.utcfromtimestamp(t).date().isoformat()
            history.append({"date": d, "close": round(float(c), 4)})

        body = {
            "symbol": symbol,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "history": history,
        }
        try:
            cache_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
        except OSError:
            pass  # in-memory response is still fine
        self._json(200, body)

    def _handle_export_transactions_csv(self):
        """Build a SAT-friendly CSV from the fetched transactions.

        Columns (Spanish, comma-delimited, RFC 4180 quoting):
          fecha (YYYY-MM-DD), hora, tipo, descripcion, ticker,
          cuenta, monto, monto_neto, comision, iva, isr_retenido,
          categoria, transaccion_id.

        Sourced from DATA/transactions.json which fetch_data.py already
        writes — no extra API calls needed. If the file isn't there yet
        (first run), return 404 with a helpful message.
        """
        import csv
        import io

        tx_file = DATA_DIR / "transactions.json"
        if not tx_file.exists():
            self._json(
                404,
                {
                    "status": "no_data",
                    "detail": "Aún no hay transactions.json — dale ⟳ Actualizar primero.",
                },
            )
            return
        try:
            tx_data = json.loads(tx_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._json(500, {"status": "error", "detail": str(e)})
            return

        rows = tx_data.get("transactions") or []
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(
            [
                "fecha",
                "hora",
                "tipo",
                "categoria",
                "descripcion",
                "ticker",
                "cuenta",
                "monto",
                "monto_neto",
                "comision",
                "iva",
                "isr_retenido_o_tax",
                "transaccion_id",
            ]
        )
        for t in rows:
            process_date = str(t.get("process_date") or "")
            fecha = process_date[:10]
            hora = process_date[11:19]
            is_buy = t.get("is_buy")
            is_sell = t.get("is_sell")
            tipo = (
                "Compra" if is_buy
                else "Venta" if is_sell
                else (t.get("transaction_type") or "Otro")
            )
            writer.writerow(
                [
                    fecha,
                    hora,
                    tipo,
                    t.get("category") or "",
                    t.get("description") or "",
                    t.get("security_id") or "",
                    t.get("account_name") or t.get("account_legacy_id") or "",
                    f"{float(t.get('amount') or 0):.4f}",
                    f"{float(t.get('net_amount') or 0):.4f}",
                    f"{float(t.get('commission') or 0):.4f}",
                    f"{float(t.get('iva') or 0):.4f}",
                    f"{float(t.get('tax') or 0):.4f}",
                    str(t.get("transaction_id") or ""),
                ]
            )

        body = buf.getvalue().encode("utf-8")
        today = tx_data.get("to_date") or ""
        filename = f"gbm-transactions-{today}.csv" if today else "gbm-transactions.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_export_page_csv(self, kind: str) -> None:
        """Per-page CSV exports — one focused subset per dashboard page.

        Spanish column names, comma-delimited, RFC 4180 quoting.
        Source JSON files live in DATA/; if a file is missing (first
        run, account doesn't support the data type) we return an empty
        CSV with only the header row — never 500.
        """
        import csv
        import io

        SPECS = {
            "ordenes": {
                "file": "orders.json",
                "list_key": "orders",
                "columns": [
                    "fecha", "side", "ticker", "descripcion", "mercado",
                    "cantidad", "monto", "estado",
                ],
                "row": lambda o: [
                    (str(o.get("operation_date") or "")[:10]),
                    "Compra" if o.get("is_buy") else "Venta" if o.get("is_sell") else "",
                    o.get("security_id") or "",
                    o.get("description") or "",
                    o.get("market_label") or o.get("market") or "",
                    f"{float(o.get('quantity') or 0):.4f}",
                    f"{float(o.get('amount') or 0):.4f}",
                    o.get("status") or "filled",
                ],
            },
            "historico": {
                # Same data file as Histórico page (every status).
                "file": "orders_all.json",
                "list_key": "orders",
                "columns": [
                    "fecha", "ticker", "side", "cantidad", "monto", "estado",
                ],
                "row": lambda o: [
                    (str(o.get("operation_date") or "")[:10]),
                    o.get("security_id") or "",
                    "Compra" if o.get("is_buy") else "Venta" if o.get("is_sell") else "",
                    f"{float(o.get('quantity') or 0):.4f}",
                    f"{float(o.get('amount') or 0):.4f}",
                    o.get("status") or "",
                ],
            },
            "dividendos": {
                "file": "dividends.json",
                "list_key": "dividends",
                "columns": [
                    "fecha", "ticker", "descripcion", "monto_bruto",
                    "isr_retenido", "monto_neto",
                ],
                "row": lambda d: [
                    (str(d.get("payment_date") or d.get("ex_date") or "")[:10]),
                    d.get("security_id") or "",
                    d.get("description") or "",
                    f"{float(d.get('gross_amount') or d.get('amount') or 0):.4f}",
                    f"{float(d.get('tax_withheld') or d.get('tax') or 0):.4f}",
                    f"{float(d.get('net_amount') or 0):.4f}",
                ],
            },
            "transacciones": {
                # Libro Diario — all cash movements, lighter than the
                # SAT-shaped /export/transactions.csv (6 cols vs 13).
                "file": "transactions.json",
                "list_key": "transactions",
                "columns": [
                    "fecha", "ticker", "descripcion", "monto",
                    "categoria", "cuenta",
                ],
                "row": lambda t: [
                    (str(t.get("process_date") or "")[:10]),
                    t.get("security_id") or "",
                    t.get("description") or "",
                    f"{float(t.get('amount') or 0):.4f}",
                    t.get("category") or "",
                    t.get("account_name") or t.get("account_legacy_id") or "",
                ],
            },
            "posiciones": {
                # Portafolio — flat list of every position across accounts.
                # positions.json shape is {<account_id>: [positions...], ...}
                "file": "positions.json",
                "list_key": None,  # not a list — see custom flatten below
                "columns": [
                    "ticker", "cuenta", "cantidad", "precio_promedio",
                    "ultimo_precio", "valor_mercado", "pnl_mxn", "pnl_pct",
                ],
                "row": lambda p: [
                    p.get("issue_id") or p.get("security_id") or "",
                    p.get("_account_name") or "",
                    f"{float(p.get('quantity') or 0):.4f}",
                    f"{float(p.get('average_price') or 0):.4f}",
                    f"{float(p.get('last_price') or 0):.4f}",
                    f"{float(p.get('market_value') or 0):.4f}",
                    f"{float(p.get('yield_value') or 0):.4f}",
                    f"{float(p.get('historical_variation_percentage') or 0):.4f}",
                ],
            },
        }

        spec = SPECS.get(kind)
        if spec is None:
            self._json(400, {"status": "bad_request", "detail": f"unknown kind: {kind}"})
            return

        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(spec["columns"])

        path = DATA_DIR / spec["file"]
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None

            if data is not None:
                if spec["list_key"]:
                    items = data.get(spec["list_key"]) or []
                else:
                    # Positions: {accountId: {name: ..., positions: [...]}, ...}
                    # Flatten + attach _account_name for the CSV row.
                    items = []
                    accounts_map = data.get("accounts") or {}
                    for acc_id, acc in (accounts_map.items() if isinstance(accounts_map, dict) else []):
                        acc_name = acc.get("name") if isinstance(acc, dict) else ""
                        for pos in (acc.get("positions") or []) if isinstance(acc, dict) else []:
                            pos = dict(pos)
                            pos["_account_name"] = acc_name
                            items.append(pos)
                    # Fallback shape: flat list under "positions"
                    if not items and isinstance(data.get("positions"), list):
                        items = data["positions"]
                for item in items:
                    try:
                        writer.writerow(spec["row"](item))
                    except (TypeError, ValueError):
                        continue

        body = buf.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="gbm-{kind}.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_settings(self):
        """Persist the configurable days-back values to .env."""
        body = self._read_json_body()
        if body is None:
            return
        updates: dict[str, str] = {}
        for js_key, (env_key, _default) in _DAYS_KEYS.items():
            raw = body.get(js_key)
            if raw is None:
                continue
            try:
                n = int(raw)
            except (TypeError, ValueError):
                self._json(400, {"status": "bad_request", "detail": f"{js_key} must be an integer"})
                return
            if not (1 <= n <= 3650):
                self._json(400, {"status": "bad_request", "detail": f"{js_key} must be 1..3650"})
                return
            updates[env_key] = str(n)
        if not updates:
            self._json(400, {"status": "bad_request", "detail": "no settings provided"})
            return
        try:
            _write_env_keys(updates)
        except OSError as e:
            self._json(500, {"status": "error", "detail": str(e)})
            return
        self._json(200, {"status": "ok"})

    def _handle_config(self):
        body = self._read_json_body()
        if body is None:
            return  # already wrote 400
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or "@" not in email:
            self._json(400, {"status": "bad_request", "detail": "valid email required"})
            return
        if not password or len(password) < 4:
            self._json(400, {"status": "bad_request", "detail": "password too short"})
            return

        # Detect whether the user is switching to a different GBM account.
        # If so, we wipe the cached session and any data files so the
        # previous account's holdings/orders don't leak into the new view,
        # and the next update forces a fresh login (TOTP modal).
        existing = _parse_env(ENV_FILE)
        previous_email = existing.get("GBM_EMAIL", "").strip().lower()
        account_changed = previous_email and previous_email != email.lower()

        try:
            _write_env(email, password)
        except OSError as e:
            self._json(500, {"status": "error", "detail": str(e)})
            return

        if account_changed:
            _wipe_session_and_data()

        # Also wipe the session if the password changed — the cached token
        # belongs to the same user, but if the user just rotated their
        # password they probably want a fresh login to verify it works.
        elif existing.get("GBM_PASSWORD") != password:
            session_path = Path.home() / ".gbm-mx" / "session.json"
            try:
                session_path.unlink(missing_ok=True)
            except OSError:
                pass

        self._json(200, {"status": "ok", "account_changed": account_changed})

    def _handle_update(self):
        body = self._read_json_body()
        if body is None:
            return

        totp_code = body.get("totp_code")
        if totp_code is not None:
            totp_code = str(totp_code).strip()
            if not (totp_code.isdigit() and len(totp_code) == 6):
                self._json(400, {"status": "bad_request", "detail": "totp must be 6 digits"})
                return

        # Optional --full: bypass incremental and force a full-window
        # fetch. Used by "Recargar todo desde cero" in the TOTP modal
        # and by the CLI for cold-start scenarios.
        full = bool(body.get("full"))

        py = str(VENV_PY) if VENV_PY.exists() else sys.executable
        cmd = [py, "-u", str(FETCH_SCRIPT), "--non-interactive"]
        if totp_code:
            cmd += ["--totp", totp_code]
        if full:
            cmd += ["--full"]

        # Atomically reserve the "running" slot. If another update is
        # already in flight (double-click, two browser tabs, etc.), refuse
        # this one — otherwise two fetch_data.py processes would race on
        # the same DATA/*.json files.
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with PROGRESS_LOCK:
            if UPDATE_STATE["running"]:
                self._json(409, {"status": "busy", "detail": "update already running"})
                return
            PROGRESS_FILE.write_text("Iniciando descarga...\n", encoding="utf-8")
            UPDATE_STATE["running"] = True
            UPDATE_STATE["started_at"] = time.time()

        captured_stderr: list[str] = []

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered
            )
        except Exception as e:
            with PROGRESS_LOCK:
                UPDATE_STATE["running"] = False
            self._json(500, {"status": "error", "detail": str(e)})
            return

        # Reader thread: append every stdout line to the progress file so
        # the frontend can show it in real time. The TOTP code (if any)
        # never appears in stdout — fetch_data.py only prints structural
        # messages ("contract: EP47NC", "Personal: 32 orders", etc.).
        def _drain_stdout():
            try:
                with PROGRESS_FILE.open("a", encoding="utf-8") as f:
                    for line in proc.stdout:  # type: ignore[union-attr]
                        f.write(line)
                        f.flush()
            except Exception:
                pass

        def _drain_stderr():
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    captured_stderr.append(line)
            except Exception:
                pass

        t_out = threading.Thread(target=_drain_stdout, daemon=True)
        t_err = threading.Thread(target=_drain_stderr, daemon=True)
        t_out.start()
        t_err.start()

        try:
            return_code = proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            with PROGRESS_LOCK:
                UPDATE_STATE["running"] = False
            self._json(504, {"status": "timeout", "detail": "fetch_data.py > 600s"})
            return
        t_out.join(timeout=2)
        t_err.join(timeout=2)

        with PROGRESS_LOCK:
            UPDATE_STATE["running"] = False

        http_status, json_status = EXIT_CODE_MAP.get(return_code, (500, "error"))
        last_stderr_line = ("".join(captured_stderr).strip().splitlines() or [""])[-1][:200]
        payload = {"status": json_status}
        if http_status == 200:
            payload["output"] = PROGRESS_FILE.read_text(encoding="utf-8")[-2000:]
        else:
            payload["detail"] = last_stderr_line
        self._json(http_status, payload)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"status": "bad_request", "detail": "invalid JSON"})
            return None

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


os.chdir(PROJECT_DIR)
with socketserver.TCPServer((BIND_HOST, PORT), Handler) as httpd:
    print(f"🚀 GBM Dashboard server running at http://localhost:{PORT}/app/index.html")
    httpd.serve_forever()
