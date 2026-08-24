"""Tests for app/places_api.py's active-flag filtering: an inactive
place (app/places_seed.py's reconcile flag, set when a place leaves the
seed) must never appear in the viewport or "near here" panel response,
even when a stale place_week row still points at it (a rotating place
drawn earlier in the week, then deactivated by a later seed reload).
"""
from __future__ import annotations

import asyncio
import json
import time

import app.places_api as places_api_module
from app.place_rotation import current_week_start
from app.places_api import places_in_viewport, places_near

WEEK = current_week_start()


def _place(conn, place_id, ref_type, lat, lon, points, rotates=0, active=1):
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (place_id, ref_type, f"ref-{place_id}", f"place-{place_id}", lat, lon,
         points, "TEST", rotates, active, int(time.time())),
    )


def test_inactive_place_excluded_from_viewport(conn, monkeypatch):
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)

    _place(conn, 1, "summit", 43.0, -116.0, points=100, rotates=0, active=1)
    _place(conn, 2, "summit", 43.01, -116.01, points=100, rotates=0, active=0)

    result = asyncio.run(places_in_viewport(north=44.0, south=42.0, west=-117.0, east=-115.0))
    ids = {p["id"] for p in json.loads(result.body)["places"]}
    assert ids == {1}


def test_inactive_place_excluded_even_with_a_stale_place_week_row(conn, monkeypatch):
    """A rotating place drawn into this week's place_week, then
    deactivated by a later seed reload, must not still show up just
    because place_week (append-only, never rewritten) still names it.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)

    _place(conn, 1, "landmark", 43.0, -116.0, points=5, rotates=1, active=0)
    _place(conn, 2, "landmark", 43.02, -116.02, points=5, rotates=1, active=1)
    conn.execute("INSERT INTO place_week(week_start, place_id) VALUES (?, ?)", (WEEK, 1))
    conn.execute("INSERT INTO place_week(week_start, place_id) VALUES (?, ?)", (WEEK, 2))

    result = asyncio.run(places_in_viewport(north=44.0, south=42.0, west=-117.0, east=-115.0))
    ids = {p["id"] for p in json.loads(result.body)["places"]}
    assert ids == {2}


def test_inactive_place_excluded_from_near_panel(conn, monkeypatch):
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)

    _place(conn, 1, "landmark", 43.0, -116.0, points=5, rotates=0, active=1)
    _place(conn, 2, "landmark", 43.001, -116.001, points=5, rotates=0, active=0)

    result = asyncio.run(places_near(lat=43.0, lon=-116.0, limit=20))
    ids = {p["id"] for p in json.loads(result.body)["places"]}
    assert ids == {1}
