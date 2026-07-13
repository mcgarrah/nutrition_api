# Multi-stage build for the Nutrition API.
#
# Stage 1 installs dependencies and bakes the GS1 GPC SQLite database from
# the bundled XML so the runtime image needs no network access at startup.
# Stage 2 is a slim runtime image with just the app, deps, and the database.

FROM python:3.13-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Build the GPC SQLite database from the bundled XML (deterministic build —
# no network fetch; the app can auto-update from GS1 at runtime if needed)
ENV PYTHONPATH=/install/lib/python3.13/site-packages
COPY scripts/ scripts/
COPY data/imports/en-v20251127.xml data/imports/en-v20251127.xml
RUN python scripts/import_gpc_xml.py \
        --xml data/imports/en-v20251127.xml \
        --db data/gpc.sqlite3


FROM python:3.13-slim

# Surface the git commit in /api/v1/version (pass with --build-arg)
ARG GIT_HASH=dev
ENV GIT_HASH=${GIT_HASH}

WORKDIR /srv/nutrition_api

COPY --from=builder /install /usr/local
COPY app/ app/
COPY scripts/ scripts/
COPY --from=builder /build/data/gpc.sqlite3 data/gpc.sqlite3

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
