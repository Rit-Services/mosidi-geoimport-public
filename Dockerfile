# Image der Import-Pipeline (CLI "pipeline", Scheduler supercronic, Demo-Karte).
# GDAL, GEOS und PROJ kommen als Wheels (pyogrio, shapely, pyproj) – keine Systeminstallation nötig.

# ---- Build: Wheels aller Abhängigkeiten
FROM python:3.12-slim AS build
# zipfile-deflate64 (Deflate64 für die INKAR-ZIP) liefert kein Linux-Wheel → hier kompilieren
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md requirements.lock ./
COPY geoimport ./geoimport
COPY webmap ./webmap
# Gepinnte Versionen aus requirements.lock, dann das Paket selbst ohne erneute Auflösung
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.lock \
    && pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# ---- Laufzeit
FROM python:3.12-slim AS runtime
ARG SUPERCRONIC_VERSION=v0.2.51
ARG SUPERCRONIC_SHA256=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123
ARG APP_VERSION=0.10.0

LABEL org.opencontainers.image.title="mosidi-geoimport" \
      org.opencontainers.image.description="Geodaten-Import-Pipeline (WFS/CSV → PostgreSQL/PostGIS)" \
      org.opencontainers.image.vendor="RIT Services GmbH, Köln" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/Rit-Services/mosidi-geoimport"

# GDAL_SKIP: GDAL erkennt Formate am Inhalt; eine als WFS-Seite getarnte OGR-VRT könnte sonst lokale
# Dateien lesen oder interne URLs abrufen (SECURITY_REVIEW S1). geoimport ergänzt den Wert beim Import.
ENV PYTHONDONTWRITEBYTECODE=1 \
    GDAL_SKIP=OGR_VRT,GPSBabel,LIBKML,KML,WFS,OAPIF \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CONFIG_DIR=/app/config \
    DATA_DIR=/data \
    GEOIMPORT_SQL_DIR=/app/sql \
    TZ=Europe/Berlin

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSLo /usr/local/bin/supercronic \
       "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/supercronic \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

WORKDIR /app
COPY config ./config
COPY sql ./sql

# Nicht als root. Auf Linux-Hosts müssen gemountete Datenverzeichnisse für UID 10001 beschreibbar sein.
RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p /data/raw /data/reports /data/state \
    && chown -R app:app /data
USER app
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=20s --retries=3 CMD ["pipeline", "check-target", "--quick"]
ENTRYPOINT ["pipeline"]
CMD ["--help"]
