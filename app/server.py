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
from pathlib import Path

PORT = 8086
PROJECT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_DIR / "app"
ENV_FILE = APP_DIR / ".env"
VENV_PY = APP_DIR / ".venv" / "bin" / "python"
FETCH_SCRIPT = APP_DIR / "fetch_data.py"

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


def _write_env(email: str, password: str) -> None:
    """Write a new .env atomically with 0600 perms.

    Preserves any other keys that were in .env (e.g. GBM_CLIENT_ID,
    GBM_LATITUDE) so the user can still tweak them by hand if needed.
    """
    existing = _parse_env(ENV_FILE)
    existing["GBM_EMAIL"] = email
    existing["GBM_PASSWORD"] = password

    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ENV_FILE.with_suffix(".env.tmp")
    lines = [
        "# Managed by the dashboard's config UI. .env is gitignored.",
        "# Edit by hand if you prefer.",
        "",
    ]
    for k, v in existing.items():
        # Wrap in single quotes so special chars ($, =, spaces) stay literal.
        # The parser strips outer quotes on read.
        lines.append(f"{k}='{v}'")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_FILE)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    def log_message(self, format, *args):
        # Reduce noise — only log non-static requests.
        path = getattr(self, "path", "")
        if any(p in path for p in ("/update", "/config")) or "code" in format:
            super().log_message(format, *args)

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------
    def do_GET(self):
        if self.path == "/config":
            env = _parse_env(ENV_FILE)
            configured = _is_configured(env)
            email = env.get("GBM_EMAIL", "") if configured else None
            self._json(200, {"configured": configured, "email": email})
            return
        super().do_GET()

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    def do_POST(self):
        if self.path == "/config":
            self._handle_config()
            return
        if self.path == "/update":
            self._handle_update()
            return
        self.send_response(404)
        self.end_headers()

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
        try:
            _write_env(email, password)
        except OSError as e:
            self._json(500, {"status": "error", "detail": str(e)})
            return
        self._json(200, {"status": "ok"})

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

        py = str(VENV_PY) if VENV_PY.exists() else sys.executable
        cmd = [py, str(FETCH_SCRIPT), "--non-interactive"]
        if totp_code:
            cmd += ["--totp", totp_code]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            self._json(504, {"status": "timeout", "detail": "fetch_data.py > 180s"})
            return
        except Exception as e:
            self._json(500, {"status": "error", "detail": str(e)})
            return

        http_status, json_status = EXIT_CODE_MAP.get(result.returncode, (500, "error"))
        last_stderr_line = (result.stderr.strip().splitlines() or [""])[-1][:200]
        payload = {"status": json_status}
        if http_status == 200:
            payload["output"] = result.stdout[-2000:]
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
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"🚀 GBM Dashboard server running at http://localhost:{PORT}/app/index.html")
    httpd.serve_forever()
