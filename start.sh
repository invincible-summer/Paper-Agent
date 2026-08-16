#!/usr/bin/env bash
# Paper Agent - One-click launch (follows the web_start_sh conventions)
# Usage: ./start.sh          Start both backend and frontend (dev)
#        ./start.sh backend   Backend only (dev)
#        ./start.sh frontend  Frontend only (dev)
#        ./start.sh prod      Production: next build && next start + uvicorn (1 worker)
#        ./start.sh stop      Stop all (works from any terminal)
# Env overrides: APP_HOST / APP_PORT (backend), FRONTEND_PORT,
#                BACKEND_URL / NEXT_PUBLIC_BACKEND_URL (backend address the
#                frontend talks to — set to the public origin when deployed).
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$ROOT/.env_conda/bin/python"
BACK_PORT_FILE=/tmp/paper_agent_backend_port
FRONT_PORT_FILE=/tmp/paper_agent_frontend_port

# Local models (embedder ~120MB + reranker ~1.1GB) are cached under
# ~/.cache/huggingface after the one-time download. Offline mode silences the
# per-load Hub metadata ping (weights are never re-downloaded). CUDA is hidden
# (models run on CPU anyway) and torch's "driver too old" UserWarning — raised
# by the driver probe even with CUDA hidden — is filtered out.
# Override any of these by setting the var before calling this script.
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-}"
export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::UserWarning:torch.cuda}"

# Ctrl+C / TERM cleanup: kill the shell children, then fuser the real ports —
# `npx next dev` is a wrapper; the actual listener is its forked next-server,
# which survives a bare kill of the wrapper and keeps the port occupied.
BACK_PID=""; FRONT_PID=""; BACK_PORT=""; FRONT_PORT=""
cleanup() {
    kill $BACK_PID $FRONT_PID 2>/dev/null || true
    sleep 1
    [ -n "$BACK_PORT" ] && fuser -k "$BACK_PORT/tcp" 2>/dev/null || true
    [ -n "$FRONT_PORT" ] && fuser -k "$FRONT_PORT/tcp" 2>/dev/null || true
}
trap cleanup INT TERM

# Pick a bindable port by ACTUAL bind probe (never ss/fuser: WSL2 mirrored
# networking can reserve host-layer ports that ss cannot see).
pick_port() {
    local preferred="$1"; shift
    local candidates=("$preferred" "$@")
    local port
    for port in "${candidates[@]}"; do
        if "$PYTHON_BIN" -c "
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(('0.0.0.0', $port)); s.close(); sys.exit(0)
except OSError:
    sys.exit(1)
" 2>/dev/null; then
            echo "$port"
            return 0
        fi
    done
    echo "$preferred"
}

# .next poisoning guard: `next build` (production) and `next dev` share
# frontend/.next. After a build, dev serves a mix of prod artifacts and dev
# chunks -> route 404s / chunk mismatch / weird runtime TypeErrors. A pure dev
# .next has NO BUILD_ID file; a production build has one — zero-false-positive
# detection, clean before starting dev.
clean_next_for_dev() {
    if [ -f "$ROOT/frontend/.next/BUILD_ID" ]; then
        echo "▶ found production build in frontend/.next (BUILD_ID); cleaning for dev"
        rm -rf "$ROOT/frontend/.next"
    fi
}

backend_addr() {
    # The address the FRONTEND uses to reach the backend (single truth source).
    echo "${NEXT_PUBLIC_BACKEND_URL:-http://localhost:$1}"
}

start_backend() {
    BACK_PORT="$(pick_port "${APP_PORT:-8000}" 8765 8123 8020)"
    echo "$BACK_PORT" > "$BACK_PORT_FILE"
    echo "▶ Backend: http://${APP_HOST:-0.0.0.0}:$BACK_PORT  (API docs: /docs)"
    cd "$ROOT/backend"
    "$PYTHON_BIN" -m uvicorn app.main:app \
        --host "${APP_HOST:-0.0.0.0}" --port "$BACK_PORT" --workers 1 &
    BACK_PID=$!
}

start_frontend() {
    clean_next_for_dev
    local bport; bport="$(cat "$BACK_PORT_FILE" 2>/dev/null || echo "${APP_PORT:-8000}")"
    if [ -z "$FRONT_PORT" ]; then
        FRONT_PORT="$(pick_port "${FRONTEND_PORT:-3000}" 3001 3030)"
    fi
    echo "$FRONT_PORT" > "$FRONT_PORT_FILE"
    echo "▶ Frontend: http://localhost:$FRONT_PORT  (backend: $bport)"
    cd "$ROOT/frontend"
    BACKEND_URL="$(backend_addr "$bport")" \
    NEXT_PUBLIC_BACKEND_URL="$(backend_addr "$bport")" \
    npx next dev -p "$FRONT_PORT" -H 0.0.0.0 &
    FRONT_PID=$!
}

start_all() {
    # Pick the frontend port before importing FastAPI so direct browser SSE
    # requests are covered by the backend's explicit CORS allow-list even
    # when the preferred port is occupied and a fallback is selected.
    FRONT_PORT="$(pick_port "${FRONTEND_PORT:-3000}" 3001 3030)"
    export FRONTEND_ORIGIN="${FRONTEND_ORIGIN:-http://localhost:$FRONT_PORT}"
    # localhost and 127.0.0.1 are distinct browser origins. Keep both explicit
    # for local E2E without ever opening CORS to "*".
    local loopback_origin="http://127.0.0.1:$FRONT_PORT"
    case ",${CORS_ORIGINS:-}," in
        *",$loopback_origin,"*) ;;
        *) export CORS_ORIGINS="${CORS_ORIGINS:+$CORS_ORIGINS,}$loopback_origin" ;;
    esac
    start_backend
    sleep 2
    start_frontend
    echo "▶ Ctrl+C to stop all"
    wait
}

start_prod() {
    # Production: single uvicorn worker (in-process session memory, circuit
    # breaker and LLM semaphores are process-local) + optimized Next build.
    # NOTE: do NOT clean .next here — the build is the product.
    FRONT_PORT="$(pick_port "${FRONTEND_PORT:-3000}" 3001 3030)"
    export FRONTEND_ORIGIN="${FRONTEND_ORIGIN:-http://localhost:$FRONT_PORT}"
    local loopback_origin="http://127.0.0.1:$FRONT_PORT"
    case ",${CORS_ORIGINS:-}," in
        *",$loopback_origin,"*) ;;
        *) export CORS_ORIGINS="${CORS_ORIGINS:+$CORS_ORIGINS,}$loopback_origin" ;;
    esac
    BACK_PORT="$(pick_port "${APP_PORT:-8000}" 8765 8123 8020)"
    echo "$BACK_PORT" > "$BACK_PORT_FILE"
    echo "$FRONT_PORT" > "$FRONT_PORT_FILE"
    cd "$ROOT/backend"
    "$PYTHON_BIN" -m uvicorn app.main:app \
        --host "${APP_HOST:-0.0.0.0}" --port "$BACK_PORT" --workers 1 &
    BACK_PID=$!
    echo "▶ Backend (pid=$BACK_PID): http://${APP_HOST:-0.0.0.0}:$BACK_PORT"
    cd "$ROOT/frontend"
    echo "▶ Building frontend..."
    BACKEND_URL="$(backend_addr "$BACK_PORT")" \
    NEXT_PUBLIC_BACKEND_URL="$(backend_addr "$BACK_PORT")" \
    npx next build
    echo "▶ Frontend (prod): http://localhost:$FRONT_PORT  (backend: $BACK_PORT)"
    echo "▶ Ctrl+C to stop all"
    BACKEND_URL="$(backend_addr "$BACK_PORT")" \
    NEXT_PUBLIC_BACKEND_URL="$(backend_addr "$BACK_PORT")" \
    npx next start -p "$FRONT_PORT" -H 0.0.0.0 &
    FRONT_PID=$!
    wait
}

stop_all() {
    # Kill by port (the service's stable identity), covering the whole
    # fallback chain — works from any terminal, no PID state needed.
    for p in 8000 8765 8123 8020 3000 3001 3030; do
        fuser -k "$p/tcp" 2>/dev/null || true
    done
    echo "■ Stopped"
}

case "${1:-all}" in
    backend)  start_backend; wait ;;
    frontend) start_frontend; wait ;;
    prod)     start_prod ;;
    all)      start_all ;;
    stop)     stop_all ;;
    *) echo "Usage: ./start.sh [all|backend|frontend|prod|stop]"; exit 1 ;;
esac
