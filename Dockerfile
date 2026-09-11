# Chess Amateur - play chess against Stockfish (depth 1) in your browser.
#
# Stockfish is downloaded during the build (official release, linux x86-64),
# so this repository does NOT need to contain the ~103 MB engine binary. The
# server itself is pure Python stdlib plus python-chess.
FROM --platform=linux/amd64 python:3.11-slim

# --- Download the Stockfish engine at build time -------------------------
# Fetch an official Stockfish release and place the binary at a stable path.
# Using the AVX2 build which runs on typical cloud CPUs (incl. Render).
ARG STOCKFISH_URL=https://github.com/official-stockfish/Stockfish/releases/download/sf_17/stockfish-ubuntu-x86-64-avx2.tar
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates xz-utils; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fL "$STOCKFISH_URL" -o /tmp/stockfish.tar; \
    mkdir -p /tmp/sf; \
    tar -xf /tmp/stockfish.tar -C /tmp/sf; \
    bin="$(find /tmp/sf -type f -name 'stockfish*' -perm -u+x | head -n 1)"; \
    if [ -z "$bin" ]; then bin="$(find /tmp/sf -type f -name 'stockfish*' | head -n 1)"; fi; \
    cp "$bin" /usr/local/bin/stockfish; \
    chmod +x /usr/local/bin/stockfish; \
    rm -rf /tmp/stockfish.tar /tmp/sf; \
    /usr/local/bin/stockfish --help >/dev/null 2>&1 || true

# --- Application ---------------------------------------------------------
WORKDIR /app

# python-chess (the only Python dependency).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY server.py engine.py ./
COPY static/ ./static/

# Point the app at the downloaded engine and default to 128 threads.
# On small hosts (e.g. Render free/starter) override with SF_THREADS=2.
ENV STOCKFISH_PATH=/usr/local/bin/stockfish \
    PORT=8000 \
    SF_THREADS=128

# Hosting platforms typically inject their own PORT; the server honors it and
# binds 0.0.0.0. 8000 is the local default.
EXPOSE 8000

CMD ["python3", "server.py"]
