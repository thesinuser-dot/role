#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Ultimate Reels AI Hunter — Container Entrypoint
#
# OLD stack (4 processes, fragile):
#   Xvfb → Fluxbox → x11vnc → websockify/noVNC → Python agent
#
# NEW stack (2 processes, rock solid):
#   Xpra start-desktop → Python agent
#
# Access the live desktop at: http://localhost:8080/
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

DISPLAY="${DISPLAY:-:99}"
SCREEN_WIDTH="${SCREEN_WIDTH:-1280}"
SCREEN_HEIGHT="${SCREEN_HEIGHT:-1024}"
SCREEN_DEPTH="${SCREEN_DEPTH:-24}"
WEB_PORT="${WEB_PORT:-8080}"
LOG_DIR="/var/log/reels-hunter"

mkdir -p "$LOG_DIR"
log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO]  $*" | tee -a "$LOG_DIR/entrypoint.log"; }
warn() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN]  $*" | tee -a "$LOG_DIR/entrypoint.log"; }
err()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*" | tee -a "$LOG_DIR/entrypoint.log" >&2; }

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
    log "Termination signal — shutting down..."
    [[ -n "${AGENT_PID:-}" ]] && kill "$AGENT_PID" 2>/dev/null \
        && log "Agent stopped."
    # Xpra has a clean shutdown command; fall back to kill
    if [[ -n "${XPRA_PID:-}" ]]; then
        xpra stop "$DISPLAY" 2>/dev/null \
            || kill "$XPRA_PID" 2>/dev/null \
            || true
        log "Xpra stopped."
    fi
    sleep 1
    log "Cleanup complete."
}
trap cleanup SIGTERM SIGINT EXIT

log "==================================================================="
log "  Reels AI Hunter — Container Starting"
log "  Display : $DISPLAY  ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}"
log "  HTML5 UI: http://localhost:${WEB_PORT}/"
log "  (open in any browser — no VNC client needed)"
log "==================================================================="

# ── Validate secrets ──────────────────────────────────────────────────────────
required_vars=("INSTAGRAM_SESSION_COOKIES" "GEMINI_API_KEY" "TELEGRAM_BOT_TOKEN" "TELEGRAM_CHAT_ID")
missing=()
for v in "${required_vars[@]}"; do
    [[ -z "${!v:-}" ]] && missing+=("$v")
done
[[ ${#missing[@]} -gt 0 ]] && warn "Missing env vars: ${missing[*]}"

# ── 1. Xpra ──────────────────────────────────────────────────────────────────
# start-desktop = full virtual desktop (like VNC) with Fluxbox as the WM.
# --html=on      = built-in HTML5 web client on WEB_PORT; no websockify needed.
# --daemon=no    = stay in foreground so we can track the PID.
# Disabled: audio, clipboard, printing, webcam, mdns — not needed in container.
# ─────────────────────────────────────────────────────────────────────────────
log "Starting Xpra desktop on display $DISPLAY, HTML5 on port $WEB_PORT..."
mkdir -p /run/xpra

xpra start-desktop "$DISPLAY" \
    --bind-tcp=0.0.0.0:${WEB_PORT} \
    --html=on \
    --start="fluxbox" \
    --daemon=no \
    --pixel-depth="${SCREEN_DEPTH}" \
    --dpi=96 \
    --desktop-scaling=off \
    --opengl=no \
    --no-mdns \
    --no-notifications \
    --no-bell \
    --no-pulseaudio \
    --speaker=off \
    --microphone=off \
    --webcam=no \
    --clipboard=no \
    --file-transfer=no \
    --printing=no \
    >"$LOG_DIR/xpra.log" 2>&1 &
XPRA_PID=$!

# ── Wait for Xpra to be ready ─────────────────────────────────────────────────
log "Waiting for Xpra to initialise (up to 30 s)..."
READY=0
for i in $(seq 1 30); do
    # Check the process is still alive first
    if ! kill -0 "$XPRA_PID" 2>/dev/null; then
        err "Xpra process died before becoming ready. Last log:"
        tail -30 "$LOG_DIR/xpra.log" >&2
        exit 1
    fi
    # xpra info exits 0 once the server is accepting connections
    if xpra info "$DISPLAY" >/dev/null 2>&1; then
        READY=1
        log "Xpra ready after ${i}s (PID=$XPRA_PID)"
        break
    fi
    sleep 1
done

if [[ $READY -eq 0 ]]; then
    err "Xpra did not become ready within 30s. Check $LOG_DIR/xpra.log"
    tail -40 "$LOG_DIR/xpra.log" >&2
    exit 1
fi

export DISPLAY="$DISPLAY"

log "==================================================================="
log "  ✅  Live desktop → http://localhost:${WEB_PORT}/"
log "      Just open that URL in Chrome/Firefox — no plugin required."
log "==================================================================="

# ── 2. Launch agent ───────────────────────────────────────────────────────────
log "Launching agent..."
DISPLAY="$DISPLAY" python3 /app/agent.py \
    > >(tee -a "$LOG_DIR/agent.log") \
    2> >(tee -a "$LOG_DIR/agent_err.log" >&2) &
AGENT_PID=$!
log "Agent started (PID=$AGENT_PID)"

wait "$AGENT_PID"
AGENT_EXIT=$?
if [[ $AGENT_EXIT -eq 0 ]]; then
    log "Agent completed successfully (exit=0)."
else
    err "Agent exited with code $AGENT_EXIT."
fi
exit $AGENT_EXIT
