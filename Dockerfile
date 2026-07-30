# Wywy-Docs test image
#
# Build:
#   docker build -t wywy-docs:test .
#
# Run tests:
#   docker run --rm --network none wywy-docs:test
#
# Run with Wywy-Website-Control docs (needed for repo-structure tests):
#   docker run --rm --network none       \
#     -v /etc/Wywy-Website-Control/docs/:/etc/Wywy-Website-Control/docs/:ro \
#     -v /etc/Wywy-Website-Control/internal/:/etc/Wywy-Website-Control/internal/:ro \
#     wywy-docs:test
#
# To keep the container around for debugging:
#   docker run --rm -it --entrypoint bash wywy-docs:test

FROM python:3.12-slim-bookworm

# ── System dependencies ─────────────────────────────────────────────
# Node.js + npm for bats (shell test framework). Git for repo-structure tests.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# ── uv (Python package manager) ─────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# ── bats (Bash test framework) ──────────────────────────────────────
RUN npm install -g bats

# ── Project setup ───────────────────────────────────────────────────
WORKDIR /app

# Copy dependency manifests and package source so uv sync can
# install the local package in editable mode.
COPY pyproject.toml uv.lock ./
COPY src/ src/

# Repo-structure tests check for docs/ and internal/ symlinks.
# Create them before uv sync so the package build can resolve them.
RUN mkdir -p /etc/Wywy-Website-Control && \
    ln -s /etc/Wywy-Website-Control/docs/ /app/docs && \
    ln -s /etc/Wywy-Website-Control/internal/ /app/internal

# Install project dependencies into .venv (locked).
RUN uv sync

# Copy the rest (tests, scripts, .git/, etc.).
# .venv/ is excluded via .dockerignore so it doesn't overwrite the
# venv created by RUN uv sync above.
COPY . ./

CMD ["./run-tests.sh"]
