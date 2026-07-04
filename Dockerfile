# ---------------------------------------------------------------------------
# Flight Card Scanner — production image
# ---------------------------------------------------------------------------
# Multi-stage build on Alpine for minimal image size.
#
# Usage:
#   docker build -t flight-card-scanner .
#   docker run -v /path/to/event:/data -p 80:80 flight-card-scanner
#
# The /data volume should contain:
#   config.json          — application configuration
#   <event_data_path>/   — images/ subdir and flight_cards.db (created automatically)
#   (optional) known_fliers.tsv
#
# All relative paths in config.json are resolved relative to /data (the
# directory containing the config file).
#
# SSL/TLS is NOT handled inside the container. Use an external reverse proxy
# (e.g. Tailscale Funnel/Serve) for HTTPS termination. See DEPLOY.md.
# ---------------------------------------------------------------------------

# =====  Stage 1: Install Node dependencies (thrustcurve-db, opencv.js)  =====
FROM node:22-alpine AS node-deps

WORKDIR /build
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc ./
# pnpm is bundled with corepack in this image
RUN corepack enable && corepack prepare pnpm@latest --activate \
    && pnpm install --frozen-lockfile


# =====  Stage 2: Build Python virtualenv with all runtime deps  =====
FROM python:3.12-alpine AS python-deps

# System libraries required to build/run Pillow (JPEG, PNG, WebP, ZLIB) and
# rapidfuzz (needs a C++ compiler at build time).
RUN apk add --no-cache \
        build-base \
        libjpeg-turbo-dev \
        zlib-dev \
        libwebp-dev \
        freetype-dev

# Create a virtualenv so we can cleanly copy it to the final stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python packages. Cryptography is omitted — SSL termination is
# handled externally (e.g. Tailscale Funnel). See DEPLOY.md.
RUN pip install --no-cache-dir \
        fastapi[standard] \
        uvicorn[standard] \
        "sqlalchemy[asyncio]" \
        aiosqlite \
        httpx \
        pydantic \
        jinja2 \
        Pillow \
        rapidfuzz \
        segno


# =====  Stage 3: Final minimal runtime image  =====
FROM python:3.12-alpine AS runtime

# Runtime-only system libraries (no -dev headers, no compiler)
RUN apk add --no-cache \
        libjpeg-turbo \
        zlib \
        libwebp \
        freetype \
        libstdc++

# Copy the pre-built virtualenv
COPY --from=python-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Application source
WORKDIR /app
COPY flight_card_scanner/ ./flight_card_scanner/

# Copy Node-installed static assets (thrustcurve-db JSON + opencv.js).
# pnpm uses a virtual store (.pnpm/) with symlinks from node_modules/, so we
# need both directories to preserve the link targets.
COPY --from=node-deps /build/flight_card_scanner/static/js/.pnpm/ \
     ./flight_card_scanner/static/js/.pnpm/
COPY --from=node-deps /build/flight_card_scanner/static/js/node_modules/ \
     ./flight_card_scanner/static/js/node_modules/

# /data is the externally-mounted volume containing config.json and event files.
# The config file lives at /data/config.json; relative paths inside it resolve
# against /data automatically (see _resolve_path in config.py).
VOLUME ["/data"]

# Default CONFIG_PATH points to the mounted volume
ENV CONFIG_PATH=/data/config.json

EXPOSE 80

# Run the application via the package entry point
CMD ["python", "-m", "flight_card_scanner"]
