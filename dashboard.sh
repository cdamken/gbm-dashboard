#!/bin/bash
# =============================================================================
# GBM Dashboard — Single orchestrator
# =============================================================================
# USO:
#   ./dashboard.sh              Arranca server + abre browser
#                               (toda la actualización se hace desde el web UI)
#   ./dashboard.sh start        Igual que el default
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

# ----------------------------------------------------------------------- server
start_server() {
    if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null; then
        echo "🌐 Server already running at http://localhost:$PORT/app/index.html"
        open "http://localhost:$PORT/app/index.html" 2>/dev/null
        return
    fi
    echo "🚀 Starting local server on port $PORT..."
    # nohup + setsid-ish disown so the server survives the parent shell
    # closing (e.g. user closes the terminal that ran ./dashboard.sh).
    nohup "$PY" "$APP_DIR/server.py" > "$SERVER_LOG" 2>&1 &
    echo $! > "$SERVER_PID"
    disown 2>/dev/null || true
    sleep 2
    echo "🌐 Server ready at http://localhost:$PORT/app/index.html"
    if [ ! -f "$APP_DIR/.env" ]; then
        echo "ℹ️  No app/.env yet — configure your GBM credentials in the browser."
    fi
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
    # `|| true` so an empty lsof result (typical: server already stopped)
    # doesn't trip `set -e` and abort a subsequent `restart`.
    local lsof_pid
    lsof_pid=$(lsof -ti:$PORT 2>/dev/null || true)
    if [ -n "$lsof_pid" ]; then
        kill -9 "$lsof_pid" 2>/dev/null || true
    fi
}

# ----------------------------------------------------------------------- status
do_status() {
    echo "📊 GBM DASHBOARD — STATUS"
    echo "========================="
    echo "Project:     $PROJECT_DIR"
    echo "Total size:  $(du -sh "$PROJECT_DIR" 2>/dev/null | cut -f1)"
    echo ""
    if [ -f "$LAST_UPDATE_FILE" ]; then
        echo "Last update: $(cat "$LAST_UPDATE_FILE")"
    else
        echo "Last update: never (open the dashboard and click ⟳ Actualizar)"
    fi
    echo ""
    echo "Data files:"
    for f in "$DATA_DIR/accounts.json" "$DATA_DIR/positions.json" \
             "$DATA_DIR/orders.json" "$DATA_DIR/orders_all.json" \
             "$DATA_DIR/dividends.json"; do
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
        echo "Server:      ⚪ stopped  (start with: ./dashboard.sh)"
    fi
}

# ----------------------------------------------------------------------- dispatch
case "${1:-}" in
    ""|start)  ensure_venv; start_server ;;
    stop)      stop_server ;;
    restart)   stop_server; ensure_venv; start_server ;;
    status)    do_status ;;
    *)
        echo "Usage: $0 [start|stop|restart|status]"
        echo ""
        echo "  (no args)  Arranca server + abre browser"
        echo "  start      Igual que el default"
        echo "  stop       Detiene el server"
        echo "  restart    stop + start"
        echo "  status     Estado del server e inventario de datos"
        echo ""
        echo "Para actualizar datos, abre el dashboard y usa ⟳ Actualizar."
        exit 1
        ;;
esac
