"""Shared pytest fixtures.

This repo had no automated test suite before "Places Worth Going"
(README's "Project status" section) -- these fixtures exist to make
that feature's own tests possible without spinning up the full app
(no MESHVIEW_BASE_URL, no admin token, no HTTP server). Config env vars
are set here, before app.config is ever imported by anything, since
pydantic-settings reads the environment at import time.
"""
from __future__ import annotations

import os
import sqlite3

os.environ.setdefault("MESHVIEW_BASE_URL", "https://example.invalid")

import pytest

from app.db import MIGRATIONS, SCHEMA


@pytest.fixture(autouse=True)
def _no_stray_legacy_places_seed(monkeypatch, tmp_path):
    """Guards every test against a REAL app/reference/places_worth_going.csv.gz
    that may be sitting on disk in this checkout (app/places_seed.py's
    "SEED LOCATION" section -- the file was `git rm --cached` 2026-09-08
    but deliberately left on disk in an existing checkout so that one
    keeps running). Without this, any test that boots the real app
    (app/db.init_db()'s backgrounded places-seed load, e.g. every
    TestClient(app) use) without itself pointing places_seed_path
    somewhere test-local would silently pick up that real, tens-of-
    megabytes, worldwide seed via places_seed._resolve_seed_path's
    legacy fallback -- turning a fast, isolated test into a real,
    multi-minute data load, times however many such tests run.

    tests/test_places_seed.py's own fallback tests re-point
    _LEGACY_DATA_PATH to their own tmp_path location within the test
    body, which simply overrides this default for the duration of that
    one test (same monkeypatch fixture instance, last setattr wins).
    """
    from app import places_seed
    monkeypatch.setattr(places_seed, "_LEGACY_DATA_PATH", str(tmp_path / "no-stray-legacy-seed.csv.gz"))


@pytest.fixture
def conn():
    """An in-memory database with the real schema (app/db.py's SCHEMA +
    MIGRATIONS), autocommit mode -- matching app/db.connect()'s own
    isolation_level=None so code under test (which issues its own
    explicit BEGIN/COMMIT, e.g. app/place_rotation.resolve_week) behaves
    exactly as it does against a real file-backed connection.
    """
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            c.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    yield c
    c.close()
