#!/usr/bin/env bash
# Container entrypoint for the Docker build of the Star Citizen Trading
# Route Optimizer. Platform-neutral: the exact same image/script works for
# a local `docker run`, a Render.com Docker web service, or (still, for
# anyone with a paid PRO plan) a Hugging Face Space -- see README.md's
# "Deployment: Render (Docker, free tier)" section for the current
# recommended free option, and its "Deployment: Hugging Face Spaces
# (Docker)" section for the HF path and why it's no longer free.
#
# This script is pure orchestration -- it runs the *exact same* entrypoints
# CLAUDE.md's "How to run" section already documents for local dev
# (`python scripts/refresh_data.py`, `uvicorn backend.main:app`,
# `streamlit run frontend/app.py`), just sequenced correctly for a single
# container where both processes have to share one exposed port. No
# backend/frontend application code is touched by this task.
#
# Port: reads `$PORT` (the convention Render -- and most modern PaaS
# platforms -- inject automatically) and falls back to `7860` (Hugging
# Face Spaces' fixed port, and a sensible default for a plain local
# `docker run` with no `-e PORT` set) when it's unset. Streamlit binds to
# this port on 0.0.0.0 (the one and only externally-visible listener); the
# backend binds to 127.0.0.1:8000 (loopback-only -- see step 2 below for
# why this is bound to loopback specifically, not just "unpublished").
#
# Order of operations:
#   1. Run a synchronous, one-off data refresh (scripts/refresh_data.py).
#      On a genuinely fresh container (first deploy, or after a rebuild
#      that wipes the ephemeral disk -- see README.md's deployment
#      section), the cache DB starts empty; without this, a visitor could
#      see an empty app for up to an hour before the backend's own
#      auto-refresh scheduler (Task 19) ever fires. A failure here is
#      logged and treated as NON-fatal on purpose: a transient external
#      API hiccup must never be a single point of failure for the whole
#      container failing to start. The scheduler and the UI's own manual
#      "Refresh data now" button both remain available once the app is up.
#   2. Start uvicorn (the backend) in the background.
#   3. Poll GET /health until the backend answers or a bounded timeout
#      elapses -- never a blind fixed sleep -- so the frontend doesn't
#      render its very first page before the backend can serve it.
#   4. `exec` streamlit in the foreground. `exec` replaces this script's
#      own shell process with streamlit (rather than running it as a
#      child), so streamlit becomes the container's PID 1 and receives
#      stop/restart signals directly from Docker/the hosting platform,
#      instead of a wrapper shell swallowing them.
#
# Process-supervision honesty note (see also README.md): this is a single
# free hobby container, not a production process supervisor. The trap
# below stops the backend if *this script* is signaled before step 4's
# `exec` runs. Once `exec` hands this PID over to streamlit, there is no
# shell left to run a trap -- the backend keeps running as a background
# process whose parent PID now happens to be streamlit's. It is not
# leaked outside the container, though: when the container is stopped,
# Docker tears down the whole container process tree together, so the
# backend process dies with it. It just doesn't get a graceful SIGTERM of
# its own in that path -- acceptable for this deployment's scope.

set -uo pipefail

cd "$(dirname "$0")/.."  # project root, so `backend`/`frontend` import exactly as documented in CLAUDE.md

export PYTHONPATH="$(pwd)${PYTHONPATH:+:${PYTHONPATH}}"

# Dynamic port: the hosting platform injects $PORT (Render always does this;
# Hugging Face Spaces does not, so 7860 -- its fixed expected port -- is the
# fallback for that case and for a plain local `docker run` with no -e PORT).
PORT="${PORT:-7860}"

echo "[start_web_container] ---- Star Citizen Trading Route Optimizer: container startup ----"
echo "[start_web_container] Working directory: $(pwd)"
echo "[start_web_container] Frontend will bind to port: ${PORT}"

echo "[start_web_container] Ensuring data/ directory exists..."
mkdir -p data

# --- 1. Synchronous initial refresh -----------------------------------------
# Deliberately not allowed to abort startup: reuses the already-reviewed
# scripts/refresh_data.py CLI (which itself calls init_db() first, so this
# also works from a totally empty data/ directory with no schema yet) and
# only logs+continues on a nonzero exit rather than failing the container.
echo "[start_web_container] Running initial data refresh (python scripts/refresh_data.py)..."
python scripts/refresh_data.py
REFRESH_EXIT_CODE=$?
if [ "$REFRESH_EXIT_CODE" -eq 0 ]; then
    echo "[start_web_container] Initial refresh succeeded."
else
    echo "[start_web_container] WARNING: initial refresh failed (exit code ${REFRESH_EXIT_CODE}). Continuing startup anyway -- the auto-refresh scheduler and the UI's manual refresh button remain available once the app is up."
fi

# --- 2. Start the backend in the background ---------------------------------
# Bound to 127.0.0.1, not 0.0.0.0: this backend is for the frontend's own
# intra-container use only (see BACKEND_BASE_URL below) and must never be a
# second externally-visible listening port. On Render specifically, the
# platform auto-detects which port to route the public URL to by scanning
# for open ports inside the container; a second port open on 0.0.0.0 is a
# real source of ambiguity there (observed: Render logged "Detected a new
# open port HTTP:8000" *after* already marking the service live on the
# correct Streamlit port, which lines up with intermittent "Not Found"
# responses landing on FastAPI's un-routed "/"). Binding to loopback removes
# the ambiguity outright -- only one port is ever visible outside this
# process tree, regardless of platform-specific port-scanning behavior.
echo "[start_web_container] Starting backend (uvicorn) on 127.0.0.1:8000 (loopback-only, not externally visible)..."
uvicorn backend.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

# Best-effort cleanup if *this script* (not yet exec'd into streamlit) gets
# signaled -- see the process-supervision note above for the exec'd case.
trap 'echo "[start_web_container] Caught signal, stopping backend (pid ${BACKEND_PID})..."; kill "${BACKEND_PID}" 2>/dev/null' INT TERM

# --- 3. Wait for the backend to actually be ready, bounded ------------------
BACKEND_READY_TIMEOUT_SECONDS=60
echo "[start_web_container] Waiting up to ${BACKEND_READY_TIMEOUT_SECONDS}s for backend readiness (GET /health)..."
BACKEND_READY=0
SECONDS_WAITED=0
while [ "$SECONDS_WAITED" -lt "$BACKEND_READY_TIMEOUT_SECONDS" ]; do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "[start_web_container] ERROR: backend process exited before becoming ready."
        break
    fi
    if python -c "
import sys
import urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)
except Exception:
    sys.exit(1)
sys.exit(0)
" >/dev/null 2>&1; then
        BACKEND_READY=1
        echo "[start_web_container] Backend is ready (after ~${SECONDS_WAITED}s)."
        break
    fi
    sleep 1
    SECONDS_WAITED=$((SECONDS_WAITED + 1))
done

if [ "$BACKEND_READY" -ne 1 ]; then
    echo "[start_web_container] WARNING: backend did not report healthy within ${BACKEND_READY_TIMEOUT_SECONDS}s. Starting the frontend anyway; it will show connection errors until the backend recovers."
fi

# --- 4. Start the frontend in the foreground (becomes the container's main process) ---
export BACKEND_BASE_URL="http://localhost:8000"
echo "[start_web_container] Starting frontend (streamlit) on 0.0.0.0:${PORT}..."
exec streamlit run frontend/app.py --server.port "${PORT}" --server.address 0.0.0.0 --server.headless true
