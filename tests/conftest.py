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
