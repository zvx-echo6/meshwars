"""FastAPI application entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import mount
from .config import settings
from .db import init_db
from .ingest import Ingestor
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

    app.state.client = client
    app.state.ingestor = ingestor
    app.state.ingest_task = task

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
        await client.aclose()


app = FastAPI(title="meshwars", lifespan=lifespan)
mount(app)


@app.get("/health")
async def health():
    return {"ok": True}
