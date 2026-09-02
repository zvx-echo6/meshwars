"""FastAPI application entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from .api import mount
from .auth import http_exception_as_error_body
from .checkin import CheckinPoller
from .config import settings
from .db import connect, init_db
from .freqmapper_ingest import FreqMapperIngestor, load_freqmapper_config
from .ingest import Ingestor
from .mc_ingest import McIngestor
from .meshview_client import MeshviewClient
from .mqtt_subscriber import MqttSubscriber

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup: meshview=%s db=%s", settings.meshview_url, settings.db_path)
    init_db()

    # Which upstream source paints the Meshtastic board -- DB-backed now
    # (app/db.py's freqmapper_config, admin-editable through
    # app/admin_ops.py's /api/admin/paint), not settings.mt_paint_source
    # -- see that column's own comment in app/db.py. Read here, after
    # init_db() (so the seed from settings has already run on a fresh
    # install), and logged once, up front, at the loudest point in
    # startup so it is obvious from the logs alone which one is live at
    # boot, without having to correlate it against either connector's
    # own per-cycle log lines. Both Ingestor.run_forever and
    # FreqMapperIngestor.run_forever log it again themselves whenever it
    # CHANGES later, since an operator can flip it at runtime and this
    # one-time startup line would otherwise go stale.
    _conn = connect()
    try:
        _paint_source = load_freqmapper_config(_conn)["mt_paint_source"]
    finally:
        _conn.close()
    log.info("meshtastic paint source: %s", _paint_source)

    client = MeshviewClient()
    ingestor = Ingestor(client)
    task = asyncio.create_task(ingestor.run_forever(), name="ingest")

    mc_ingestor = McIngestor()
    if settings.mc_ingest_enabled:
        await mc_ingestor.start()

    # FreqMapper (app/freqmapper_ingest.py): an alternative Meshtastic
    # paint source to meshview's position-packet feed above. Started
    # UNCONDITIONALLY -- run_forever() no longer gates on
    # freqmapper_config.enabled and exit early the way it once gated on
    # settings.freqmapper_enabled; `enabled` is a runtime toggle now
    # (like settings.mc_ingest_enabled's own gate below, but living
    # inside the task's own loop rather than guarding whether the task
    # is created at all), so the loop has to keep running to notice an
    # operator flipping it on later. A fresh install with FreqMapper
    # never configured still starts this task; it simply does nothing
    # each cycle until enabled and an api_key are both set.
    freqmapper_ingestor = FreqMapperIngestor()
    freqmapper_task = asyncio.create_task(freqmapper_ingestor.run_forever(), name="freqmapper-ingest")

    # Net check-ins (app/checkin.py). Shares `client` (the same
    # MeshviewClient the position-packet Ingestor above already holds)
    # for its default Meshtastic connector, rather than opening a second
    # connection pool to the same meshview host -- see CheckinPoller's
    # docstring. Started UNCONDITIONALLY, unlike before checkin_net/
    # checkin_config existed (this used to be gated on
    # settings.checkin_enabled at process startup) -- the whole point of
    # moving that flag into checkin_config is that an admin can toggle
    # it at runtime with no restart, which only works if the loop is
    # always running to notice the toggle. The loop itself checks
    # checkin_config.enabled on every cycle and does nothing when it is
    # off (see CheckinPoller._poll_once) -- a fresh install with no nets
    # configured yet still starts a background task, but that task polls
    # nothing until an admin adds a net and turns it on.
    checkin_poller = CheckinPoller(client)
    await checkin_poller.start()

    # MQTT connector kind (app/mqtt_subscriber.py). Started unconditionally,
    # same reasoning as checkin_poller just above: it reconciles which
    # brokers to hold open against checkin_net's current enabled 'mqtt'
    # rows on its own interval, so a fresh install with no mqtt nets
    # configured yet still starts the task, but it simply holds no
    # connections until an admin adds one. A wholly separate background
    # task from checkin_poller, not folded into it -- see that module's
    # docstring for why a persistent broker subscription has no business
    # living inside a 30-second poll loop.
    mqtt_subscriber = MqttSubscriber()
    await mqtt_subscriber.start()

    app.state.client = client
    app.state.ingestor = ingestor
    app.state.ingest_task = task
    app.state.mc_ingestor = mc_ingestor
    app.state.checkin_poller = checkin_poller
    app.state.mqtt_subscriber = mqtt_subscriber
    app.state.freqmapper_ingestor = freqmapper_ingestor
    app.state.freqmapper_task = freqmapper_task

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
        freqmapper_ingestor.stop()
        freqmapper_task.cancel()
        try:
            await freqmapper_task
        except (asyncio.CancelledError, Exception):
            pass
        if settings.mc_ingest_enabled:
            await mc_ingestor.stop()
        await checkin_poller.stop()
        await mqtt_subscriber.stop()
        await client.aclose()


# docs_url=None: FastAPI registers its own interactive Swagger UI at
# /docs by default, at construction time -- Starlette's router matches
# routes in the order they were added, so that built-in route would
# always win over app/api.py's own /docs page (mounted later, in
# mount() below) and silently swallow every request to it. Nothing here
# relies on the auto-generated Swagger UI: the public API is documented
# by hand at /api (frontend/api.html).
# redoc_url=None and openapi_url=None: the auto-generated OpenAPI
# document enumerates every route in the app -- including the ingest,
# join, and admin surfaces (player/delete, player/disable, node/remove,
# issue_key, revoke, and the rest) -- none of which are public API, even
# though they're token-guarded. Closing openapi_url is what actually
# matters: with it left set, the JSON stayed reachable at /openapi.json
# even with both UI routes (/docs, /redoc) disabled.
app = FastAPI(
    title="meshwars",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# Every hand-rolled error response in this app -- long before
# app/auth.py existed, and everywhere that hasn't been touched by it --
# is a JSONResponse shaped {"error": "..."}. Starlette's own default
# HTTPException handler instead renders {"detail": "..."}. app/auth.py's
# shared authentication dependency (see that module) is the first place
# in this codebase to raise HTTPException rather than building a
# JSONResponse by hand, specifically so it can be used as a real FastAPI
# dependency (Depends(...) can only short-circuit a request via a raised
# exception, not a returned value) -- but doing that must not change the
# JSON body a client already depending on {"error": ...} sees.
# http_exception_as_error_body (defined in app/auth.py, next to the code
# that's the only thing in this codebase raising HTTPException) is that
# translation, registered once, app-wide (keyed on fastapi.HTTPException
# -- the exact type app/auth.py raises -- which takes priority over
# FastAPI's own default handler, registered on the
# starlette.exceptions.HTTPException base class it subclasses, since
# lookup walks the exception's MRO from its exact type outward and this
# is the more specific match) so any HTTPException app/auth.py raises
# renders in the same shape every existing error response already uses.
# Nothing before app/auth.py ever raised one, so this has no effect on
# any route this refactor didn't touch.
app.add_exception_handler(HTTPException, http_exception_as_error_body)


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
