"""Tests for app/admin_ops.py's observation_source admin surface:
GET/POST /api/admin/observation/sources[/create|/update|/delete] and the
validation helper behind them, _validate_source_fields (and the shared
helpers it draws on -- _validate_label/_validate_kind_and_protocol/
_validate_connector_url/_validate_mqtt_fields).

Same "FastAPI-around-one-router" + file-backed sqlite + real admin
session shape tests/test_admin_ops_checkin.py already uses for
app/admin_ops.py's own router (admin_router = app.admin_ops.router) --
a real session held by an account with role='admin' is what proves a
request reaches the actual handler rather than just re-demonstrating
_role_guard's own guard-off 404.
"""
from __future__ import annotations

import asyncio
import base64
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app.admin_ops import router as admin_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
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
    """A TestClient already authenticated as an admin-role account --
    every route under test requires _role_guard()'s default need="admin".
    """
    conn = sqlite3.connect(db_path)
    cur = conn.execute("INSERT INTO account(created_at, role) VALUES (?, 'admin')", (NOW,))
    account_id = cur.lastrowid
    # _role_guard() (app/admin_api.py) requires an ACTIVE TOTP enrollment
    # on every call for any role-holding account, not just at claim time
    # -- a role alone would 403 every route under test (see that
    # function's own docstring).
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, 'unused', ?, ?)",
        (account_id, NOW, NOW),
    )
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    c = TestClient(app)
    raw_token = asyncio.run(create_session(account_id, device_label=None))
    c.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return c


def _meshview_source(**overrides) -> dict:
    body = {
        "label": "Test Meshview Source",
        "kind": "meshview",
        "connector_url": "https://meshview.example",
    }
    body.update(overrides)
    return body


def _mqtt_meshtastic_source(**overrides) -> dict:
    body = {
        "label": "Public Meshtastic Broker",
        "kind": "mqtt_meshtastic",
        "connector_url": "mqtt://mqtt.meshtastic.org:1883",
        "topic_root": "msh/US",
        "channel": "LongFast",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------
# create / list / update / delete round trip
# ---------------------------------------------------------------------

def test_create_list_update_delete_round_trip(client):
    create_resp = client.post("/api/admin/observation/sources/create", json=_meshview_source())
    assert create_resp.status_code == 201
    created = create_resp.json()
    source_id = created["id"]
    assert created["label"] == "Test Meshview Source"
    assert created["kind"] == "meshview"
    assert created["protocol"] == "mt"
    assert created["enabled"] is True or created["enabled"] == 1

    list_resp = client.get("/api/admin/observation/sources")
    assert list_resp.status_code == 200
    sources = list_resp.json()["sources"]
    assert len(sources) == 1
    assert sources[0]["id"] == source_id

    update_resp = client.post(
        "/api/admin/observation/sources/update",
        json={**_meshview_source(label="Renamed Source"), "id": source_id},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["label"] == "Renamed Source"

    list_after_update = client.get("/api/admin/observation/sources").json()["sources"]
    assert list_after_update[0]["label"] == "Renamed Source"

    delete_resp = client.post(
        "/api/admin/observation/sources/delete",
        json={"id": source_id, "label": "Renamed Source"},
    )
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"id": source_id, "deleted": True}

    assert client.get("/api/admin/observation/sources").json()["sources"] == []


# ---------------------------------------------------------------------
# kind / protocol validation
# ---------------------------------------------------------------------

def test_kind_not_in_allowed_set_is_400(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json=_meshview_source(kind="not_a_real_kind"),
    )
    assert resp.status_code == 400


def test_submitted_protocol_disagreeing_with_kind_is_400(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        # meshview -> protocol 'mt' -- submitting 'mc' must be rejected,
        # not silently overridden.
        json=_meshview_source(protocol="mc"),
    )
    assert resp.status_code == 400


def test_submitted_protocol_agreeing_with_kind_is_accepted(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json=_meshview_source(protocol="mt"),
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------
# connector_url scheme
# ---------------------------------------------------------------------

def test_mqtt_kind_requires_mqtt_scheme_connector_url(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json=_mqtt_meshtastic_source(connector_url="https://not-a-broker.example"),
    )
    assert resp.status_code == 400


def test_mqtt_kind_accepts_mqtts_scheme(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json=_mqtt_meshtastic_source(connector_url="mqtts://mqtt.meshtastic.org:8883"),
    )
    assert resp.status_code == 201


def test_http_kind_requires_http_scheme_connector_url(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json=_meshview_source(connector_url="mqtt://wrong-scheme.example"),
    )
    assert resp.status_code == 400


def test_http_kind_accepts_https_scheme(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json=_meshview_source(connector_url="https://meshview.example"),
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------
# mqtt_meshtastic requires topic_root AND channel
# ---------------------------------------------------------------------

def test_mqtt_meshtastic_blank_channel_is_400(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json=_mqtt_meshtastic_source(channel=""),
    )
    assert resp.status_code == 400


def test_mqtt_meshtastic_blank_topic_root_is_400(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json=_mqtt_meshtastic_source(topic_root=""),
    )
    assert resp.status_code == 400


def test_mqtt_meshtastic_with_both_fields_is_accepted(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json=_mqtt_meshtastic_source(),
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------
# plain mqtt: channel is OPTIONAL
# ---------------------------------------------------------------------

def test_plain_mqtt_blank_channel_is_accepted(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json={
            "label": "Private Broker",
            "kind": "mqtt",
            "connector_url": "mqtt://broker.private:1883",
            "channel": "",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["channel"] == ""


# ---------------------------------------------------------------------
# secrets: keep-on-blank, explicit clear, invalid base64
# ---------------------------------------------------------------------

def test_blank_secrets_on_update_keep_stored_values(client):
    create_resp = client.post(
        "/api/admin/observation/sources/create",
        json=_mqtt_meshtastic_source(
            broker_password="s3cret", channel_key=base64.b64encode(bytes(range(16))).decode(),
        ),
    )
    source_id = create_resp.json()["id"]
    assert create_resp.json()["has_broker_password"] is True
    assert create_resp.json()["has_channel_key"] is True

    # Blank submission on update -- must NOT wipe the stored secrets.
    update_resp = client.post(
        "/api/admin/observation/sources/update",
        json={**_mqtt_meshtastic_source(broker_password="", channel_key=""), "id": source_id},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["has_broker_password"] is True
    assert update_resp.json()["has_channel_key"] is True

    # And the real value really is unchanged (checked directly against
    # storage, since the API never echoes it back -- see _scrub_secrets).
    conn = sqlite3.connect(db.settings.db_path)
    row = conn.execute(
        "SELECT broker_password, channel_key FROM observation_source WHERE id = ?", (source_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "s3cret"
    assert row[1] == base64.b64encode(bytes(range(16))).decode()


def test_clear_broker_password_and_channel_key_blank_them(client):
    create_resp = client.post(
        "/api/admin/observation/sources/create",
        json=_mqtt_meshtastic_source(
            broker_password="s3cret", channel_key=base64.b64encode(bytes(range(16))).decode(),
        ),
    )
    source_id = create_resp.json()["id"]

    update_resp = client.post(
        "/api/admin/observation/sources/update",
        json={
            **_mqtt_meshtastic_source(),
            "id": source_id,
            "clear_broker_password": True,
            "clear_channel_key": True,
        },
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["has_broker_password"] is False
    assert update_resp.json()["has_channel_key"] is False

    conn = sqlite3.connect(db.settings.db_path)
    row = conn.execute(
        "SELECT broker_password, channel_key FROM observation_source WHERE id = ?", (source_id,)
    ).fetchone()
    conn.close()
    assert row[0] == ""
    assert row[1] == ""


def test_invalid_base64_channel_key_is_400(client):
    resp = client.post(
        "/api/admin/observation/sources/create",
        json=_mqtt_meshtastic_source(channel_key="not valid base64 at all !!"),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------
# secrets never leave the process
# ---------------------------------------------------------------------

def test_list_create_update_responses_never_contain_raw_secrets(client):
    create_resp = client.post(
        "/api/admin/observation/sources/create",
        json=_mqtt_meshtastic_source(
            broker_password="s3cret", channel_key=base64.b64encode(bytes(range(16))).decode(),
        ),
    )
    created = create_resp.json()
    source_id = created["id"]
    assert "broker_password" not in created
    assert "channel_key" not in created
    assert created["has_broker_password"] is True
    assert created["has_channel_key"] is True

    list_resp = client.get("/api/admin/observation/sources").json()
    for s in list_resp["sources"]:
        assert "broker_password" not in s
        assert "channel_key" not in s
        assert "has_broker_password" in s
        assert "has_channel_key" in s

    update_resp = client.post(
        "/api/admin/observation/sources/update",
        json={**_mqtt_meshtastic_source(), "id": source_id},
    ).json()
    assert "broker_password" not in update_resp
    assert "channel_key" not in update_resp
    assert "has_broker_password" in update_resp
    assert "has_channel_key" in update_resp


# ---------------------------------------------------------------------
# delete with a mismatched label
# ---------------------------------------------------------------------

def test_delete_with_non_matching_label_is_409(client):
    create_resp = client.post("/api/admin/observation/sources/create", json=_meshview_source())
    source_id = create_resp.json()["id"]

    resp = client.post(
        "/api/admin/observation/sources/delete",
        json={"id": source_id, "label": "Totally Wrong Label"},
    )
    assert resp.status_code == 409

    # Not actually deleted.
    sources = client.get("/api/admin/observation/sources").json()["sources"]
    assert len(sources) == 1
