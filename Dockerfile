FROM python:3.12-slim

# Run as an unprivileged user, not root. Nothing this app does needs
# privilege: it binds 8090 (above 1024), reads a read-only tiles mount
# and writes one SQLite database. A public instance ingesting wardriving
# data off the internet has no business running as uid 0, and root-owned
# files landing in an operator's ./data directory are files they then
# need sudo to back up, move or delete.
#
# uid/gid 1000 is the first regular account on a typical Linux desktop
# or server, so the default lines up with whoever cloned the repo. An
# operator whose account is a different uid overrides it at RUN time
# with PUID/PGID (see docker-compose.yml) -- the image does not have to
# be rebuilt for that, which is why the ownership below is set by number
# as well as by name.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" meshwars && \
    useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin meshwars

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY frontend/ ./frontend/

# Everything COPYed above was written as root during the build. The app
# never writes to /app at runtime, but CPython does: it drops __pycache__
# next to each module on first import, and an unwritable /app makes every
# start pay full compile cost. Compile once here, then hand the tree to
# the app user so the caches are usable and no root-owned file is left
# where the unprivileged process has to work.
RUN python -m compileall -q /app/app && \
    chown -R "${APP_UID}:${APP_GID}" /app /home/meshwars

EXPOSE 8090

ENV PYTHONUNBUFFERED=1
ENV HOME=/home/meshwars
ENV DB_PATH=/data/game.db
# Where the terrain/overlay PMTiles archives (USFS roads+trails, public
# lands) live -- a bind mount, not the meshwars-data volume, see
# docker-compose.yml. Absent entirely on a checkout that hasn't set one
# up yet; the /tiles mount in app/api.py just doesn't appear then.
ENV TILES_DIR=/tiles-data

# Whatever is mounted at /data must be writable by the runtime user, and
# the DIRECTORY must be, not just the database file: SQLite in WAL mode
# creates game.db-wal and game.db-shm alongside it and cannot open the
# database read-write without being able to create them. `chown -R
# 1000:1000 ./data` on the host is the one-time fix for an install
# migrating from the old root-owned layout. /tiles-data is mounted
# read-only and needs nothing.
USER meshwars

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:8090/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
