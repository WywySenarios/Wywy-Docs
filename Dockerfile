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

# Copy dependency manifests first (layer caching).
COPY pyproject.toml ./
COPY src/ src/

# Install project dependencies into .venv.
RUN uv sync

# Copy the rest (tests, scripts, docs, .git/, etc.).
# .venv/ is excluded via .dockerignore so it doesn't overwrite the
# venv created by RUN uv sync above.
COPY . ./

# Repo-structure tests check for docs/ and internal/ symlinks.
# Create them pointing at a mountable path.  When the host
# /etc/Wywy-Website-Control/{docs,internal} are not mounted, the
# target directory is empty — repo-structure checks 7-8 pass (they
# test the symlink itself, not content), but any content-dependent
# test must create its own fixture data (the existing tests already do).
RUN rm -f /app/docs /app/internal && \
    mkdir -p /etc/Wywy-Website-Control && \
    ln -s /etc/Wywy-Website-Control/docs/ /app/docs && \
    ln -s /etc/Wywy-Website-Control/internal/ /app/internal

CMD ["./run-tests.sh"]
