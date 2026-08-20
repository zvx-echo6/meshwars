"""FastAPI application entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

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
mount(app)


@app.get("/health")
async def health():
    return {"ok": True}
