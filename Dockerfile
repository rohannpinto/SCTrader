# Docker image for the free-hosted build of the Star Citizen Trading Route
# Optimizer: packages the existing FastAPI backend + Streamlit frontend into
# a single container so a free-tier host only needs to expose one
# process/port. Zero application code changes -- see README.md's
# "Deployment: Render (Docker, free tier)" section (the current recommended
# free option) and its "Deployment: Hugging Face Spaces (Docker)" section
# (still workable, just no longer free as of Hugging Face's July 2026 policy
# change) for the full rationale, and scripts/start_web_container.sh for the
# startup orchestration this image's CMD runs.
#
# Base image: Python 3.12, matching this project's own .venv (Python
# 3.12.5 -- see requirements.txt's header comment) as closely as a stock
# Docker Hub tag allows.
FROM python:3.12-slim

WORKDIR /app

# Install pinned dependencies first, from requirements.txt alone, so this
# (slow) layer stays cached across rebuilds that only touch application
# code below.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code. Deliberately explicit per-directory COPYs
# (not `COPY . .`) so nothing else in the repo (tests/, .claude/, .git/,
# local .env, etc.) can end up in the image by accident -- belt-and-
# suspenders alongside .dockerignore.
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/

# backend/models/db.py's create_db_engine() already creates data/'s parent
# directory on demand (`path.parent.mkdir(parents=True, exist_ok=True)`)
# the first time the DB engine is created, and scripts/refresh_data.py
# calls init_db() before anything else touches the database -- verified
# manually against a fresh, empty directory (see this task's report), not
# just assumed. This line is a visible belt-and-suspenders convenience on
# top of that, not a requirement for correctness.
RUN mkdir -p /app/data

RUN chmod +x scripts/start_web_container.sh

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# Informational only -- Docker's EXPOSE doesn't actually bind or publish
# anything at runtime. The port actually bound is controlled at runtime by
# the $PORT env var (see scripts/start_web_container.sh), defaulting to
# 7860 if $PORT is unset. Render injects $PORT automatically; Hugging Face
# Spaces expects the fixed port declared in README.md's YAML frontmatter
# (7860, matching this default). The backend's own port (8000) is
# deliberately not EXPOSEd here -- it only ever needs to be reachable from
# inside this same container.
EXPOSE 7860

CMD ["bash", "scripts/start_web_container.sh"]
