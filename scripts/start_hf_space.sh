#!/usr/bin/env bash
# Container entrypoint for the Hugging Face Space (Docker SDK) build of the
# Star Citizen Trading Route Optimizer.
#
# This script is pure orchestration -- it runs the *exact same* entrypoints
# CLAUDE.md's "How to run" section already documents for local dev
# (`python scripts/refresh_data.py`, `uvicorn backend.main:app`,
# `streamlit run frontend/app.py`), just sequenced correctly for a single
# container where both processes have to share one exposed port. No
# backend/frontend application code is touched by this task.
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
#      stop/restart signals directly from Docker/Hugging Face, instead of
#      a wrapper shell swallowing them.
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

echo "[start_hf_space] ---- Star Citizen Trading Route Optimizer: container startup ----"
echo "[start_hf_space] Working directory: $(pwd)"

echo "[start_hf_space] Ensuring data/ directory exists..."
mkdir -p data

# --- 1. Synchronous initial refresh -----------------------------------------
# Deliberately not allowed to abort startup: reuses the already-reviewed
# scripts/refresh_data.py CLI (which itself calls init_db() first, so this
# also works from a totally empty data/ directory with no schema yet) and
# only logs+continues on a nonzero exit rather than failing the container.
echo "[start_hf_space] Running initial data refresh (python scripts/refresh_data.py)..."
python scripts/refresh_data.py
REFRESH_EXIT_CODE=$?
if [ "$REFRESH_EXIT_CODE" -eq 0 ]; then
    echo "[start_hf_space] Initial refresh succeeded."
else
    echo "[start_hf_space] WARNING: initial refresh failed (exit code ${REFRESH_EXIT_CODE}). Continuing startup anyway -- the auto-refresh scheduler and the UI's manual refresh button remain available once the app is up."
fi

# --- 2. Start the backend in the background ---------------------------------
echo "[start_hf_space] Starting backend (uvicorn) on 0.0.0.0:8000..."
uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Best-effort cleanup if *this script* (not yet exec'd into streamlit) gets
# signaled -- see the process-supervision note above for the exec'd case.
trap 'echo "[start_hf_space] Caught signal, stopping backend (pid ${BACKEND_PID})..."; kill "${BACKEND_PID}" 2>/dev/null' INT TERM

# --- 3. Wait for the backend to actually be ready, bounded ------------------
BACKEND_READY_TIMEOUT_SECONDS=60
echo "[start_hf_space] Waiting up to ${BACKEND_READY_TIMEOUT_SECONDS}s for backend readiness (GET /health)..."
BACKEND_READY=0
SECONDS_WAITED=0
while [ "$SECONDS_WAITED" -lt "$BACKEND_READY_TIMEOUT_SECONDS" ]; do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "[start_hf_space] ERROR: backend process exited before becoming ready."
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
        echo "[start_hf_space] Backend is ready (after ~${SECONDS_WAITED}s)."
        break
    fi
    sleep 1
    SECONDS_WAITED=$((SECONDS_WAITED + 1))
done

if [ "$BACKEND_READY" -ne 1 ]; then
    echo "[start_hf_space] WARNING: backend did not report healthy within ${BACKEND_READY_TIMEOUT_SECONDS}s. Starting the frontend anyway; it will show connection errors until the backend recovers."
fi

# --- 4. Start the frontend in the foreground (becomes the container's main process) ---
export BACKEND_BASE_URL="http://localhost:8000"
echo "[start_hf_space] Starting frontend (streamlit) on 0.0.0.0:7860..."
exec streamlit run frontend/app.py --server.port 7860 --server.address 0.0.0.0 --server.headless true
