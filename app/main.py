"""FastAPI application entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from .api import mount
from .checkin import CheckinPoller
from .config import settings
from .db import init_db
from .ingest import Ingestor
from .mc_ingest import McIngestor
from .meshview_client import MeshviewClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup: meshview=%s db=%s", settings.meshview_url, settings.db_path)
    init_db()

    client = MeshviewClient()
    ingestor = Ingestor(client)
    task = asyncio.create_task(ingestor.run_forever(), name="ingest")

    mc_ingestor = McIngestor()
    if settings.mc_ingest_enabled:
        await mc_ingestor.start()

    # Net check-ins (app/checkin.py). Shares `client` (the same
    # MeshviewClient the position-packet Ingestor above already holds)
    # for its Meshtastic feed, rather than opening a second connection
    # pool to the same meshview host -- see CheckinPoller's docstring.
    # Constructed unconditionally, same as McIngestor above, so
    # app.state.checkin_poller always exists for app/checkin_api.py's
    # node-picker endpoint to read a (possibly still-empty) directory
    # cache from even when checkin_enabled is false; only its background
    # poll loop is gated by the flag -- a fresh install must not start
    # polling live.mwmesh.com/meshview for a feature it was never
    # configured for.
    checkin_poller = CheckinPoller(client)
    if settings.checkin_enabled:
        await checkin_poller.start()

    app.state.client = client
    app.state.ingestor = ingestor
    app.state.ingest_task = task
    app.state.mc_ingestor = mc_ingestor
    app.state.checkin_poller = checkin_poller

    try:
        yield
    finally:
        log.info("shutdown: stopping ingest")
        ingestor.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        if settings.mc_ingest_enabled:
            await mc_ingestor.stop()
        if settings.checkin_enabled:
            await checkin_poller.stop()
        await client.aclose()


app = FastAPI(title="meshwars", lifespan=lifespan)

# The board routes return highly repetitive JSON -- thousands of cell
# records sharing the same handful of keys and team names -- and every
# open map tab re-fetches them on a timer. Compressing costs a little
# CPU per response and saves an order of magnitude on the wire, which is
# the right trade for the one route that dominates this site's traffic.
# minimum_size skips the small routes (/scores, /config, /season), where
# the header overhead would not pay for itself.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Cross-origin GETs, for the public read API (app/public_api.py). Every
# route it serves was already reachable without a key, so allowing a
# browser to read them changes what is possible for a dashboard, not
# what is possible for an attacker -- CORS restricts browsers, not
# clients. Methods are limited to GET and HEAD so the same permission
# never extends to the ingest or admin routes, which do write and which
# authenticate by header.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD"],
    allow_headers=["*"],
    max_age=3600,
)
mount(app)


@app.get("/health")
async def health():
    return {"ok": True}
