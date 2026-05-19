#!/bin/bash
# =============================================================================
# GBM Dashboard — Single orchestrator
# =============================================================================
# USO:
#   ./dashboard.sh              Smart update + arranca server + abre browser
#   ./dashboard.sh update       Igual que ↑ (alias explícito)
#   ./dashboard.sh start        Solo arranca el server (no toca datos)
#   ./dashboard.sh stop         Detiene el server
#   ./dashboard.sh restart      stop + start
#   ./dashboard.sh status       Inventario, fechas, estado del server
# =============================================================================

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$PROJECT_DIR/app"
DATA_DIR="$PROJECT_DIR/DATA"
PORT=8086
VENV_DIR="$APP_DIR/.venv"
PY="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

LAST_UPDATE_FILE="$DATA_DIR/last_update.date"
LOG_FILE="$DATA_DIR/last_update.log"
SERVER_LOG="$DATA_DIR/server.log"
SERVER_PID="$DATA_DIR/server.pid"

mkdir -p "$DATA_DIR"
cd "$PROJECT_DIR"

# ----------------------------------------------------------------------- helpers
ensure_venv() {
    if [ ! -x "$PY" ]; then
        echo "🔧 Setting up virtualenv at $VENV_DIR..."
        python3 -m venv "$VENV_DIR"
        "$PIP" install --quiet --upgrade pip
    fi

    if ! "$PY" -c "import gbm_mx_api" 2>/dev/null; then
        echo "📦 Installing gbm-mx-api from GitHub..."
        "$PIP" install --quiet \
            "gbm-mx-api[cli] @ git+https://github.com/cdamken/gbm-mx-api.git"
    fi
}

ensure_env() {
    if [ ! -f "$APP_DIR/.env" ]; then
        cat <<EOF

⚠️  Missing app/.env — create it from app/.env.example:

   cp $APP_DIR/.env.example $APP_DIR/.env
   $EDITOR $APP_DIR/.env

EOF
        exit 1
    fi
}

mfa_banner() {
    cat <<'BANNER'

┌──────────────────────────────────────────────────────────────────────┐
│  🔐  GBM SECURITY CODE                                               │
│                                                                      │
│  If you see "TOTP code:" below, your session expired.                │
│                                                                      │
│  📱 Open your authenticator app → 6-digit code for GBM+              │
│  ⏱  ~30 seconds before the code rotates.                             │
└──────────────────────────────────────────────────────────────────────┘

BANNER
}

# ----------------------------------------------------------------------- fetch
fetch_data() {
    echo "📊 Fetching live data from GBM..."
    "$PY" "$APP_DIR/fetch_data.py"
}

cleanup() {
    local removed
    removed=$(find "$PROJECT_DIR" \( -name '.DS_Store' -o -name '*.tmp' -o -name '*.partial' \) -type f -print -delete 2>/dev/null | wc -l | tr -d ' ')
    if [ "$removed" -gt 0 ]; then
        echo "🧹 Cleaned $removed leftover files."
    fi
}

# ----------------------------------------------------------------------- server
start_server() {
    if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null; then
        echo "🌐 Server already running at http://localhost:$PORT/app/index.html"
        open "http://localhost:$PORT/app/index.html" 2>/dev/null
        return
    fi
    echo "🚀 Starting local server on port $PORT..."
    "$PY" "$APP_DIR/server.py" > "$SERVER_LOG" 2>&1 &
    echo $! > "$SERVER_PID"
    sleep 2
    echo "🌐 Server ready at http://localhost:$PORT/app/index.html"
    open "http://localhost:$PORT/app/index.html" 2>/dev/null
}

stop_server() {
    if [ -f "$SERVER_PID" ]; then
        local pid
        pid=$(cat "$SERVER_PID")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            echo "🛑 Server stopped (PID $pid)."
        fi
        rm -f "$SERVER_PID"
    fi
    local lsof_pid
    lsof_pid=$(lsof -ti:$PORT 2>/dev/null)
    if [ -n "$lsof_pid" ]; then
        kill -9 "$lsof_pid" 2>/dev/null
    fi
}

# ----------------------------------------------------------------------- actions
do_update() {
    ensure_venv
    ensure_env

    set -o pipefail
    {
        mfa_banner
        fetch_data
        date +"%Y-%m-%d %H:%M:%S" > "$LAST_UPDATE_FILE"
    } 2>&1 | tee "$LOG_FILE"
    local rc=${PIPESTATUS[0]}
    set +o pipefail
    if [ "$rc" -ne 0 ]; then
        echo "❌ Update failed. See log: $LOG_FILE"
        exit "$rc"
    fi
    cleanup
    summarize
    start_server
}

do_status() {
    echo "📊 GBM DASHBOARD — STATUS"
    echo "========================="
    echo "Project:     $PROJECT_DIR"
    echo "Total size:  $(du -sh "$PROJECT_DIR" 2>/dev/null | cut -f1)"
    echo ""
    if [ -f "$LAST_UPDATE_FILE" ]; then
        echo "Last update: $(cat "$LAST_UPDATE_FILE")"
    else
        echo "Last update: never (run ./dashboard.sh)"
    fi
    echo ""
    echo "Data files:"
    for f in "$DATA_DIR/accounts.json" "$DATA_DIR/positions.json" "$DATA_DIR/summary.json"; do
        if [ -f "$f" ]; then
            local mtime size
            mtime=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$f")
            size=$(du -h "$f" | cut -f1)
            printf "  %-35s  %6s  %s\n" "$(basename "$f")" "$size" "$mtime"
        else
            printf "  %-35s  (missing)\n" "$(basename "$f")"
        fi
    done
    echo ""
    if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo "Server:      🟢 RUNNING  (http://localhost:$PORT/app/index.html)"
    else
        echo "Server:      ⚪ stopped  (start with: ./dashboard.sh start)"
    fi
}

summarize() {
    echo ""
    echo "✅ Update complete."
    if [ -f "$LAST_UPDATE_FILE" ]; then
        echo "    Date saved:  $(cat "$LAST_UPDATE_FILE") → $LAST_UPDATE_FILE"
    fi
    echo "    Log:         $LOG_FILE"
}

# ----------------------------------------------------------------------- dispatch
case "${1:-}" in
    "")        do_update ;;
    update)    do_update ;;
    start)     ensure_venv; start_server ;;
    stop)      stop_server ;;
    restart)   stop_server; start_server ;;
    status)    do_status ;;
    *)
        echo "Usage: $0 [update|start|stop|restart|status]"
        echo ""
        echo "  (no args)  Smart update + arranca server (alias de 'update')"
        echo "  update     Fetch data + arranca server"
        echo "  start      Just start the local HTTP server"
        echo "  stop       Stop the server"
        echo "  restart    stop + start"
        echo "  status     Show data files, last update, server state"
        exit 1
        ;;
esac
