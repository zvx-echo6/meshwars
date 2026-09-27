"""Tests for the "Connections" feature: app/db.py's new `community`
table and the community_id columns it adds to checkin_net/
observation_source, app/checkin.py's net_window_text() formatter, the
community CRUD + net<->source conversion routes in app/admin_ops.py,
and the public GET /api/about/communities in app/mc_api.py.

Same "FastAPI-around-one-router" + file-backed sqlite + real admin
session shape tests/test_observation_sources.py already uses.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app.admin_ops import router as admin_router
from app.auth import http_exception_as_error_body
from app.checkin import net_window_text
from app.db import MIGRATIONS, SCHEMA
from app.mc_api import router as mc_router
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())


def _init_schema(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(db.settings, "db_path", path)
    return path


@pytest.fixture
def client(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.execute("INSERT INTO account(created_at, role) VALUES (?, 'admin')", (NOW,))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, 'unused', ?, ?)",
        (account_id, NOW, NOW),
    )
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(mc_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    c = TestClient(app)
    raw_token = asyncio.run(create_session(account_id, device_label=None))
    c.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return c


def _corescope_net(**overrides) -> dict:
    body = {
        "label": "Test Net", "kind": "corescope", "connector_url": "https://cs.example",
        "channel": "general", "weekday": 2, "start_hour": 17, "end_hour": 23,
        "timezone": "America/Boise", "start_date": "2026-01-01",
    }
    body.update(overrides)
    return body


def _meshview_source(**overrides) -> dict:
    body = {"label": "Test Source", "kind": "meshview", "connector_url": "https://meshview.example"}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------
# net_window_text -- pinned against the 5 real prod rows (frontend/
# about.html's current wording), plus the all-day/partial-day split.
# ---------------------------------------------------------------------

def test_window_text_mountain_west_mesh():
    net = {"weekday": 2, "start_hour": 17, "end_hour": 23, "timezone": "America/Boise"}
    assert net_window_text(net) == "Wednesdays, 5:00pm to midnight Mountain time"


def test_window_text_freq51_meshview():
    net = {"weekday": 2, "start_hour": 17, "end_hour": 23, "timezone": "America/Boise"}
    assert net_window_text(net) == "Wednesdays, 5:00pm to midnight Mountain time"


def test_window_text_colorado_mesh_all_day():
    net = {"weekday": 3, "start_hour": 0, "end_hour": 23, "timezone": "America/Boise"}
    assert net_window_text(net) == "all day Thursday, Mountain time"


def test_window_text_central_oregon_all_day_pacific():
    net = {"weekday": 0, "start_hour": 0, "end_hour": 23, "timezone": "America/Los_Angeles"}
    assert net_window_text(net) == "all day Monday, Pacific time"


def test_window_text_ntx_mesh_central():
    net = {"weekday": 1, "start_hour": 18, "end_hour": 23, "timezone": "America/Chicago"}
    assert net_window_text(net) == "Tuesdays, 6:00pm to midnight Central time"


def test_window_text_unknown_timezone_falls_back_to_iana_name():
    net = {"weekday": 4, "start_hour": 9, "end_hour": 17, "timezone": "Europe/London"}
    assert net_window_text(net) == "Fridays, 9:00am to 6:00pm Europe/London time"


def test_window_text_noon_boundary():
    net = {"weekday": 5, "start_hour": 9, "end_hour": 11, "timezone": "America/Denver"}
    assert net_window_text(net) == "Saturdays, 9:00am to noon Mountain time"


# ---------------------------------------------------------------------
# migration idempotency
# ---------------------------------------------------------------------

def test_community_table_and_community_id_columns_exist_after_init(tmp_path, monkeypatch):
    path = str(tmp_path / "fresh.db")
    monkeypatch.setattr(db.settings, "db_path", path)
    db.init_db()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "community" in tables
    net_cols = {r["name"] for r in conn.execute("PRAGMA table_info(checkin_net)")}
    source_cols = {r["name"] for r in conn.execute("PRAGMA table_info(observation_source)")}
    assert "community_id" in net_cols
    assert "community_id" in source_cols
    conn.close()


def test_migration_is_idempotent_on_existing_db(tmp_path, monkeypatch):
    """init_db() re-runs the MIGRATIONS loop on every boot -- a second
    call against an already-migrated database (this feature's ALTERs
    included) must be a true no-op, not an error."""
    path = str(tmp_path / "existing.db")
    monkeypatch.setattr(db.settings, "db_path", path)
    db.init_db()
    db.init_db()  # must not raise
    conn = sqlite3.connect(path)
    row_count = conn.execute("SELECT COUNT(*) FROM community").fetchone()[0]
    assert row_count == 0
    conn.close()


# ---------------------------------------------------------------------
# community CRUD
# ---------------------------------------------------------------------

def test_community_create_list_update_delete_round_trip(client):
    create_resp = client.post(
        "/api/admin/communities/create",
        json={"name": "Mountain West Mesh", "region": "Idaho", "url": "https://mwmesh.com",
              "display_order": 1},
    )
    assert create_resp.status_code == 201
    community_id = create_resp.json()["id"]

    list_resp = client.get("/api/admin/communities").json()["communities"]
    assert len(list_resp) == 1
    assert list_resp[0]["name"] == "Mountain West Mesh"

    update_resp = client.post(
        "/api/admin/communities/update",
        json={"id": community_id, "name": "Mountain West Mesh Renamed", "display_order": 2},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "Mountain West Mesh Renamed"

    delete_resp = client.post(
        "/api/admin/communities/delete",
        json={"id": community_id, "name": "Mountain West Mesh Renamed"},
    )
    assert delete_resp.status_code == 200
    assert delete_resp.json()["unlinked_nets"] == 0
    assert delete_resp.json()["unlinked_sources"] == 0

    assert client.get("/api/admin/communities").json()["communities"] == []


def test_community_create_requires_name(client):
    resp = client.post("/api/admin/communities/create", json={"name": ""})
    assert resp.status_code == 400


def test_community_delete_with_wrong_name_is_409(client):
    community_id = client.post(
        "/api/admin/communities/create", json={"name": "Colorado Mesh"}
    ).json()["id"]
    resp = client.post(
        "/api/admin/communities/delete", json={"id": community_id, "name": "Wrong Name"}
    )
    assert resp.status_code == 409


def test_community_delete_unlinks_but_does_not_delete_nets_and_sources(client):
    community_id = client.post(
        "/api/admin/communities/create", json={"name": "Colorado Mesh"}
    ).json()["id"]
    net_id = client.post(
        "/api/admin/checkin/nets/create", json=_corescope_net(community_id=community_id)
    ).json()["id"]
    source_id = client.post(
        "/api/admin/observation/sources/create",
        json=_meshview_source(community_id=community_id),
    ).json()["id"]

    resp = client.post(
        "/api/admin/communities/delete", json={"id": community_id, "name": "Colorado Mesh"}
    )
    assert resp.status_code == 200
    assert resp.json()["unlinked_nets"] == 1
    assert resp.json()["unlinked_sources"] == 1

    nets = client.get("/api/admin/checkin/nets").json()["nets"]
    sources = client.get("/api/admin/observation/sources").json()["sources"]
    assert len(nets) == 1 and nets[0]["id"] == net_id and nets[0]["community_id"] is None
    assert len(sources) == 1 and sources[0]["id"] == source_id and sources[0]["community_id"] is None


# ---------------------------------------------------------------------
# community_id on nets/sources
# ---------------------------------------------------------------------

def test_net_create_with_valid_community_id_succeeds_and_is_returned(client):
    community_id = client.post(
        "/api/admin/communities/create", json={"name": "FREQ51"}
    ).json()["id"]
    resp = client.post("/api/admin/checkin/nets/create", json=_corescope_net(community_id=community_id))
    assert resp.status_code == 201
    assert resp.json()["community_id"] == community_id
    assert "window_text" in resp.json()

    list_resp = client.get("/api/admin/checkin/nets").json()["nets"]
    assert list_resp[0]["community_id"] == community_id
    assert list_resp[0]["window_text"] == "Wednesdays, 5:00pm to midnight Mountain time"


def test_net_create_with_nonexistent_community_id_is_400(client):
    resp = client.post("/api/admin/checkin/nets/create", json=_corescope_net(community_id=999999))
    assert resp.status_code == 400


def test_net_create_with_null_community_id_succeeds(client):
    resp = client.post("/api/admin/checkin/nets/create", json=_corescope_net())
    assert resp.status_code == 201
    assert resp.json()["community_id"] is None


def test_source_create_with_valid_and_invalid_community_id(client):
    community_id = client.post(
        "/api/admin/communities/create", json={"name": "NE Ohio"}
    ).json()["id"]
    ok = client.post(
        "/api/admin/observation/sources/create",
        json=_meshview_source(community_id=community_id),
    )
    assert ok.status_code == 201
    assert ok.json()["community_id"] == community_id

    bad = client.post(
        "/api/admin/observation/sources/create",
        json=_meshview_source(community_id=999999),
    )
    assert bad.status_code == 400


def test_net_update_can_change_community_id(client):
    c1 = client.post("/api/admin/communities/create", json={"name": "A"}).json()["id"]
    c2 = client.post("/api/admin/communities/create", json={"name": "B"}).json()["id"]
    net_id = client.post(
        "/api/admin/checkin/nets/create", json=_corescope_net(community_id=c1)
    ).json()["id"]

    resp = client.post(
        "/api/admin/checkin/nets/update",
        json={**_corescope_net(community_id=c2), "id": net_id},
    )
    assert resp.status_code == 200
    assert resp.json()["community_id"] == c2


# ---------------------------------------------------------------------
# conversion: net -> source, source -> net
# ---------------------------------------------------------------------

def test_convert_net_to_source_preserves_fields_and_drops_window(client):
    community_id = client.post(
        "/api/admin/communities/create", json={"name": "Central Oregon"}
    ).json()["id"]
    net_id = client.post(
        "/api/admin/checkin/nets/create",
        json=_corescope_net(label="Convert Me", community_id=community_id),
    ).json()["id"]

    resp = client.post("/api/admin/checkin/nets/convert-to-source", json={"id": net_id})
    assert resp.status_code == 201
    body = resp.json()
    assert body["label"] == "Convert Me"
    assert body["kind"] == "corescope"
    assert body["protocol"] == "mc"
    assert body["community_id"] == community_id
    assert "weekday" not in body
    assert "start_hour" not in body

    # Old net is gone, new source exists.
    assert client.get("/api/admin/checkin/nets").json()["nets"] == []
    sources = client.get("/api/admin/observation/sources").json()["sources"]
    assert len(sources) == 1
    assert sources[0]["id"] == body["id"]


def test_convert_net_to_source_nonexistent_id_is_404(client):
    resp = client.post("/api/admin/checkin/nets/convert-to-source", json={"id": 999999})
    assert resp.status_code == 404


def test_convert_source_to_net_requires_schedule_fields(client):
    source_id = client.post(
        "/api/admin/observation/sources/create", json=_meshview_source(label="Promote Me")
    ).json()["id"]

    # Missing schedule fields entirely -- weekday is not a valid int.
    resp = client.post(
        "/api/admin/observation/sources/convert-to-net", json={"id": source_id}
    )
    assert resp.status_code == 400
    # Source must still exist -- rejected before any delete.
    assert len(client.get("/api/admin/observation/sources").json()["sources"]) == 1


def test_convert_source_to_net_with_schedule_succeeds(client):
    community_id = client.post(
        "/api/admin/communities/create", json={"name": "NE Ohio Meshtastic"}
    ).json()["id"]
    source_id = client.post(
        "/api/admin/observation/sources/create",
        json=_meshview_source(label="Promote Me", community_id=community_id),
    ).json()["id"]

    resp = client.post(
        "/api/admin/observation/sources/convert-to-net",
        json={
            "id": source_id, "hashtag": "#neome", "weekday": 4,
            "start_hour": 18, "end_hour": 20, "timezone": "America/New_York",
            "start_date": "",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["label"] == "Promote Me"
    assert body["community_id"] == community_id
    assert body["hashtag"] == "#neome"
    assert body["window_text"] == "Fridays, 6:00pm to 9:00pm Eastern time"

    assert client.get("/api/admin/observation/sources").json()["sources"] == []
    nets = client.get("/api/admin/checkin/nets").json()["nets"]
    assert len(nets) == 1 and nets[0]["id"] == body["id"]


def test_convert_round_trip_does_not_touch_award_history(client, db_path):
    """A net's earned check-in history (mc_checkin_award) must survive a
    net -> source conversion exactly as it already survives a plain net
    delete (see admin_checkin_net_delete's own docstring) -- the award
    row's net_id may end up pointing at an id no longer present in
    checkin_net, which is the SAME already-accepted state a plain
    delete already produces, never a foreign-key violation or a lost
    row.
    """
    net_id = client.post("/api/admin/checkin/nets/create", json=_corescope_net()).json()["id"]

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_season(id, protocol, started_at, ends_at, status) "
        "VALUES (1, 'mc', ?, ?, 'active')",
        (NOW, NOW + 86400),
    )
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, "
        " message_id, awarded_at, net_id) VALUES (1, 1, '2026-01-07', 1.0, 'mc', 'm1', ?, ?)",
        (NOW, net_id),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/admin/checkin/nets/convert-to-source", json={"id": net_id})
    assert resp.status_code == 201

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM mc_checkin_award WHERE net_date = '2026-01-07'").fetchone()
    conn.close()
    assert row is not None
    assert row["net_id"] == net_id  # untouched, same dangling-id state a plain delete leaves


# ---------------------------------------------------------------------
# public GET /api/about/communities
# ---------------------------------------------------------------------

def test_about_communities_shape_and_ordering(client):
    c1 = client.post(
        "/api/admin/communities/create",
        json={"name": "Zeta Mesh", "region": "OR", "blurb": "Wardriving live.",
              "url": "https://zeta.example", "contact_url": "https://zeta.example/contact",
              "display_order": 2},
    ).json()["id"]
    c2 = client.post(
        "/api/admin/communities/create",
        json={"name": "Alpha Mesh", "display_order": 1},
    ).json()["id"]

    client.post(
        "/api/admin/checkin/nets/create",
        json=_corescope_net(label="Alpha Net", community_id=c2),
    )

    resp = client.get("/api/about/communities")
    assert resp.status_code == 200
    data = resp.json()
    assert [c["name"] for c in data] == ["Alpha Mesh", "Zeta Mesh"]  # display_order first

    alpha = data[0]
    assert alpha["protocols"] == ["mc"]
    assert len(alpha["nets"]) == 1
    assert alpha["nets"][0] == {
        "protocol": "mc", "window_text": "Wednesdays, 5:00pm to midnight Mountain time",
        "channel": "general",
    }

    zeta = data[1]
    assert zeta["blurb"] == "Wardriving live."
    assert zeta["nets"] == []
    assert zeta["protocols"] == []


def test_about_communities_excludes_shown_on_about_false(client):
    client.post(
        "/api/admin/communities/create",
        json={"name": "Hidden Mesh", "shown_on_about": False},
    )
    resp = client.get("/api/about/communities")
    assert resp.json() == []


def test_about_communities_excludes_disabled_nets_and_sources(client):
    community_id = client.post(
        "/api/admin/communities/create", json={"name": "Quiet Mesh"}
    ).json()["id"]
    client.post(
        "/api/admin/checkin/nets/create",
        json=_corescope_net(community_id=community_id, enabled=False),
    )
    client.post(
        "/api/admin/observation/sources/create",
        json=_meshview_source(community_id=community_id, enabled=False),
    )
    resp = client.get("/api/about/communities")
    data = resp.json()
    assert data[0]["nets"] == []
    assert data[0]["protocols"] == []


def test_about_communities_never_exposes_secrets_or_connector_fields(client):
    community_id = client.post(
        "/api/admin/communities/create", json={"name": "Secret Mesh"}
    ).json()["id"]
    client.post(
        "/api/admin/checkin/nets/create",
        json=_corescope_net(community_id=community_id, connector_url="https://secret.example"),
    )
    client.post(
        "/api/admin/observation/sources/create",
        json={
            "label": "MQTT Source", "kind": "mqtt", "connector_url": "mqtt://broker.private:1883",
            "broker_username": "brokeruser", "broker_password": "s3cret",
            "community_id": community_id,
        },
    )

    resp = client.get("/api/about/communities")
    raw = resp.text
    for forbidden in (
        "s3cret", "brokeruser", "broker_password", "broker_username", "channel_key",
        "topic_root", "connector_url", "https://secret.example", "mqtt://broker.private",
        "\"id\":", "\"kind\":",
    ):
        assert forbidden not in raw, f"leaked forbidden field/value: {forbidden!r}"
