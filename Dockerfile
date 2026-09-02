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

# uvicorn's own X-Forwarded-For/X-Forwarded-Proto handling (see
# https://www.uvicorn.org/settings/#http -- ProxyHeadersMiddleware).
# Independent of, and in addition to, this app's OWN client-IP
# resolution (app/client_ip.py, gated on settings.trusted_proxies): this
# flag is what makes request.url.scheme read "https" instead of "http"
# behind Caddy's TLS-terminating reverse proxy, which nothing in this
# codebase checks today but any future redirect/secure-cookie logic
# would otherwise get silently wrong. It can ALSO resolve
# request.client.host itself, using the identical "walk the
# X-Forwarded-For chain from the right, stop at the first hop that
# isn't a trusted proxy" algorithm app/client_ip.py implements -- see
# that module's docstring for why both layers exist rather than relying
# on just this one (short version: this is a process-startup flag, not
# an ordinary env var an operator can change without recreating the
# container, and it is not something a unit test can exercise the way
# app/client_ip.py's own tests do).
#
# --forwarded-allow-ips defaults to uvicorn's own safe default,
# 127.0.0.1, which trusts nothing in this container's actual deployment
# (Caddy runs in a different container, reached over the LAN, never
# over loopback -- see docker-compose.yml's port publish). An operator
# whose reverse proxy connects from somewhere else must override
# FORWARDED_ALLOW_IPS via the environment (docker-compose.yml passes it
# through) to that proxy's real address -- the same address that
# belongs in TRUSTED_PROXIES (app/config.py's settings.trusted_proxies)
# for the app's own rate limiters to see real caller addresses. Left at
# the conservative default here, rather than pointed at this
# deployment's own proxy, because that address is private infrastructure
# and this Dockerfile is public.
ENV FORWARDED_ALLOW_IPS=127.0.0.1

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

# Shell form (rather than the previous exec-form JSON array) so
# $FORWARDED_ALLOW_IPS -- set by ENV above, overridable at run time via
# docker-compose.yml -- is actually substituted; `exec` hands the shell's
# PID straight to uvicorn afterwards, so signal handling (SIGTERM on
# `docker stop`, `restart: unless-stopped`'s use of it) is exactly the
# same as it was with the plain exec-form CMD this replaces -- there is
# no long-lived /bin/sh left in between to swallow anything.
CMD ["/bin/sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8090 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\""]
