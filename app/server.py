"""Local HTTP server for the GBM Dashboard.

Endpoints:

  GET  /app/*.html        — dashboard HTML
  GET  /DATA/*.json       — data fetched by fetch_data.py
  POST /update            — request a refresh. Body: {} or {"totp_code": "123456"}
                            Returns one of:
                              200 {"status": "ok", "output": "..."}
                              401 {"status": "mfa_required"}   (sesión expirada)
                              401 {"status": "mfa_invalid"}    (TOTP malo)
                              401 {"status": "auth_failed"}    (credenciales malas)
                              500 {"status": "error", "detail": "..."}
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
VENV_PY = PROJECT_DIR / "app" / ".venv" / "bin" / "python"
FETCH_SCRIPT = PROJECT_DIR / "app" / "fetch_data.py"


# Map fetch_data.py exit codes to (HTTP status, JSON status string).
EXIT_CODE_MAP = {
    0:  (200, "ok"),
    10: (401, "mfa_required"),
    11: (401, "mfa_invalid"),
    12: (401, "auth_failed"),
    20: (502, "api_error"),
    30: (500, "config_error"),
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    # Quiet down the default access-log noise for static files.
    def log_message(self, format, *args):
        # Only log POST /update for visibility.
        if "/update" in self.requestline or "update" in format:
            super().log_message(format, *args)

    def do_POST(self):
        if self.path != "/update":
            self.send_response(404)
            self.end_headers()
            return

        # Parse JSON body (optional).
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"status": "bad_request", "detail": "invalid JSON"})
                return

        totp_code = body.get("totp_code")
        if totp_code is not None:
            totp_code = str(totp_code).strip()
            if not (totp_code.isdigit() and len(totp_code) == 6):
                self._json(400, {"status": "bad_request", "detail": "totp must be 6 digits"})
                return

        # Build subprocess command.
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

        http_status, json_status = EXIT_CODE_MAP.get(
            result.returncode, (500, "error")
        )
        # We never echo back stderr verbatim — it may contain sensitive info.
        # But sanitized: keep last line only.
        last_stderr_line = (result.stderr.strip().splitlines() or [""])[-1][:200]

        payload = {"status": json_status}
        if http_status == 200:
            payload["output"] = result.stdout[-2000:]  # truncated
        elif http_status in (401, 502, 500):
            payload["detail"] = last_stderr_line
        self._json(http_status, payload)

    # Helpers
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
