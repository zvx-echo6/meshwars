"""Tests for the READ side and admin surface of app/db.py's
mc_evasion_record -- see that table's own SCHEMA comment for the full
policy. Several things this file exists to prove that no other test
file covers:

1. app/admin_ops.py's _worth_a_look() actually CHECKS this table (a
   currently-active player's own ip_hash/node_ref against every row on
   file) and surfaces a match as its own item that FLAGS, never blocks.
   Before this existed, the table was write-only: nothing anywhere read
   it back.

2. That surfacing is DELIBERATELY SEPARATE from _attention()'s own
   "Needs attention" list -- an evasion match never carries a severity,
   is never returned inside GET /api/admin/overview's own `attention`
   array, and therefore structurally cannot flip admin.js's own nav
   badge (`list.some((a) => a.severity === 'bad')`, where `list` is
   `attention` alone -- see the integration test below that proves the
   two arrays never mix at the HTTP layer, which is what that JS
   expression actually runs against).

3. Item copy: observation, then the innocent explanation (with a
   denominator when one is cheaply available), then the consequence --
   and titles that describe data, never people.

4. Dismissal (POST /api/admin/worth-a-look/dismiss): no confirmation,
   no admin_action_log row, persisted in admin_worth_a_look_dismissal
   keyed on (player_id, signal), and survives a fresh read.

5. The minimal admin review surface (app/admin_api.py's GET
   /api/admin/evasion-records and POST /api/admin/evasion-records/delete)
   and the "mark for evasion tracking" toggle (POST
   /api/admin/player/mark-evasion / unmark-evasion) that trigger path
   #2 (app/account_api.py's _capture_evasion_record()) depends on.

The ingest-path log-only checks that ALSO read mc_evasion_record
(app/mc_ingest.py's record_ingest_identity()/its own binding-time
check) are covered in tests/test_mc_ingest_identity_capture.py instead,
next to the rest of that module's own tests. The three deletion-time
trigger paths are covered in tests/test_admin_player_delete.py and
tests/test_account_delete.py, next to _capture_evasion_record()'s other
tests.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app.admin_api import router as admin_router
from app.admin_ops import _attention, _worth_a_look, router as admin_ops_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())


def _run(coro):
    return asyncio.run(coro)


def _make_player_row(conn, display_name="Test Player", team="RED"):
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES (?, ?, ?)",
        (display_name, team, NOW),
    )
    return cur.lastrowid


def _seed_evasion_record(conn, kind, value, former_player_id=99, reason="test", recorded_at=NOW):
    conn.execute(
        "INSERT INTO mc_evasion_record(former_player_id, kind, value, reason, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (former_player_id, kind, value, reason, recorded_at),
    )


# =========================================================================
# _worth_a_look(): the READ side -- flags, never blocks, never severity
# =========================================================================

def test_ip_hash_match_surfaces_a_worth_a_look_item(conn):
    player_id = _make_player_row(conn)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (NOW, player_id),
    )
    _seed_evasion_record(conn, "ip_hash", "shared-hash", former_player_id=42)

    items = _worth_a_look(conn)
    matches = [i for i in items if i["player_id"] == player_id]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["signal"] == "evasion_ip_hash"
    assert "shares a network address" in entry["title"]
    assert "previously disabled" in entry["title"]
    # Never a verdict word anywhere in the surfaced copy.
    for banned in ("evader", "cheater", "banned", "guilty"):
        assert banned not in entry["title"].lower()
        assert banned not in entry["detail"].lower()


def test_node_ref_match_surfaces_a_worth_a_look_item(conn):
    player_id = _make_player_row(conn)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) "
        "VALUES ('mc', 'aaaa1111', ?, ?)",
        (player_id, NOW),
    )
    _seed_evasion_record(conn, "node_ref", "mc:aaaa1111", former_player_id=42)

    items = _worth_a_look(conn)
    matches = [i for i in items if i["player_id"] == player_id]
    assert len(matches) == 1
    assert matches[0]["signal"] == "evasion_node_ref"
    assert "previously bound to a player" in matches[0]["title"]


def test_no_match_means_no_item(conn):
    player_id = _make_player_row(conn)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'unrelated-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (NOW, player_id),
    )
    _seed_evasion_record(conn, "ip_hash", "some-other-hash", former_player_id=42)

    assert _worth_a_look(conn) == []


def test_empty_evasion_record_table_returns_empty_list(conn):
    _make_player_row(conn)
    assert _worth_a_look(conn) == []


def test_no_item_ever_carries_a_severity_field(conn):
    """The explicit constraint: no code path in this block may ever
    assign severity 'bad' -- enforced here by asserting the field does
    not exist at all, so a future edit that quietly adds one (bad or
    otherwise) fails this test."""
    player_id = _make_player_row(conn)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (NOW, player_id),
    )
    _seed_evasion_record(conn, "ip_hash", "shared-hash", former_player_id=42)

    items = _worth_a_look(conn)
    assert items
    for i in items:
        assert "severity" not in i


def test_copy_order_is_observation_then_innocent_reading_then_consequence(conn):
    player_id = _make_player_row(conn)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (NOW, player_id),
    )
    _seed_evasion_record(conn, "ip_hash", "shared-hash", former_player_id=42)

    detail = [i for i in _worth_a_look(conn) if i["player_id"] == player_id][0]["detail"]
    observation_pos = detail.find("also used by a player disabled")
    innocent_pos = detail.find("shared household")
    consequence_pos = detail.find("Nothing to do")
    assert -1 < observation_pos < innocent_pos < consequence_pos


def test_denominator_counts_other_currently_active_players_sharing_the_address(conn):
    """The cheapest de-escalation available: a widely-shared address
    reads very differently from a one-to-one match."""
    p1 = _make_player_row(conn, display_name="P1")
    p2 = _make_player_row(conn, display_name="P2")
    p3 = _make_player_row(conn, display_name="P3")
    for pid in (p1, p2, p3):
        conn.execute(
            "INSERT INTO mc_ingest_request_log"
            "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
            " client_family, ping_count, type_counts) "
            "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
            (NOW, pid),
        )
    _seed_evasion_record(conn, "ip_hash", "shared-hash", former_player_id=42)

    entry = [i for i in _worth_a_look(conn) if i["player_id"] == p1][0]
    # p2 and p3 are the "other" active players sharing this address.
    assert "2 other" in entry["detail"]


def test_no_denominator_sentence_when_no_one_else_shares_the_address(conn):
    player_id = _make_player_row(conn)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (NOW, player_id),
    )
    _seed_evasion_record(conn, "ip_hash", "shared-hash", former_player_id=42)

    entry = [i for i in _worth_a_look(conn) if i["player_id"] == player_id][0]
    assert "other active player" not in entry["detail"]


def test_disabled_players_are_never_surfaced(conn):
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at, disabled_at) VALUES (?, ?, ?, ?)",
        ("Disabled Player", "RED", NOW, NOW),
    )
    player_id = cur.lastrowid
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (NOW, player_id),
    )
    _seed_evasion_record(conn, "ip_hash", "shared-hash", former_player_id=42)

    assert _worth_a_look(conn) == []


def test_dismissed_item_no_longer_surfaces(conn):
    player_id = _make_player_row(conn)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (NOW, player_id),
    )
    _seed_evasion_record(conn, "ip_hash", "shared-hash", former_player_id=42)
    assert [i for i in _worth_a_look(conn) if i["player_id"] == player_id]

    conn.execute(
        "INSERT INTO admin_worth_a_look_dismissal(player_id, signal, dismissed_at) VALUES (?, ?, ?)",
        (player_id, "evasion_ip_hash", NOW),
    )
    assert not [i for i in _worth_a_look(conn) if i["player_id"] == player_id]


def test_dismissal_is_scoped_to_one_signal_not_the_whole_player(conn):
    """Dismissing the ip_hash observation must not also hide an
    unrelated node_ref observation for the same player."""
    player_id = _make_player_row(conn)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (NOW, player_id),
    )
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) "
        "VALUES ('mc', 'aaaa1111', ?, ?)",
        (player_id, NOW),
    )
    _seed_evasion_record(conn, "ip_hash", "shared-hash", former_player_id=42)
    _seed_evasion_record(conn, "node_ref", "mc:aaaa1111", former_player_id=43)
    conn.execute(
        "INSERT INTO admin_worth_a_look_dismissal(player_id, signal, dismissed_at) VALUES (?, ?, ?)",
        (player_id, "evasion_ip_hash", NOW),
    )

    signals = {i["signal"] for i in _worth_a_look(conn) if i["player_id"] == player_id}
    assert signals == {"evasion_node_ref"}


# =========================================================================
# _attention() must NEVER carry this signal -- the whole point of the split
# =========================================================================

def test_attention_never_contains_an_evasion_entry(conn):
    player_id = _make_player_row(conn)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (NOW, player_id),
    )
    _seed_evasion_record(conn, "ip_hash", "shared-hash", former_player_id=42)

    entries = _attention(conn, directory=[])
    assert not [e for e in entries if "evasion" in e["kind"]]


# =========================================================================
# Admin surface -- HTTP routes (app/admin_api.py + app/admin_ops.py)
# =========================================================================

class FakeIngestor:
    def __init__(self) -> None:
        self.invalidated: list[int] = []

    def invalidate_player(self, player_id: int) -> None:
        self.invalidated.append(player_id)


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
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(admin_ops_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    app.state.mc_ingestor = FakeIngestor()
    return TestClient(app)


def _make_account(path: str, *, role: str | None = None) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO account(created_at, role) VALUES (?, ?)", (int(time.time()), role)
    )
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return account_id


def _login_as(client, db_path, *, role: str) -> int:
    account_id = _make_account(db_path, role=role)
    if role is not None:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
            "VALUES (?, 'unused', ?, ?)",
            (account_id, int(time.time()), int(time.time())),
        )
        conn.commit()
        conn.close()
    raw_token = _run(create_session(account_id, device_label=None))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return account_id


def _make_player(path: str, *, display_name="Tester", team="RED") -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES (?, ?, ?)",
        (display_name, team, int(time.time())),
    )
    conn.commit()
    player_id = cur.lastrowid
    conn.close()
    return player_id


def _row(db_path: str, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


def _count(db_path: str, table: str, col: str, value) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (value,)).fetchone()[0]
    conn.close()
    return n


def _seed_record(db_path: str, kind="ip_hash", value="somehash", former_player_id=42, reason="test") -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO mc_evasion_record(former_player_id, kind, value, reason, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (former_player_id, kind, value, reason, int(time.time())),
    )
    conn.commit()
    record_id = cur.lastrowid
    conn.close()
    return record_id


# ---- GET /api/admin/overview: attention and worth_a_look never mix --------

def test_overview_keeps_attention_and_worth_a_look_as_separate_arrays(client, db_path):
    """The structural proof that a Worth a look item cannot turn the
    admin.js nav badge red: that badge is computed in renderAttention()
    as `list.some((a) => a.severity === 'bad')` where `list` is
    `d.attention` ALONE (see loadOverview()'s own
    `renderAttention(d.attention)` call in frontend/admin.js) --
    `d.worth_a_look` is a separate top-level key that function never
    receives. This test pins the CONTRACT that JS code depends on: an
    evasion match appears in `worth_a_look`, never in `attention`, and
    no entry in `attention` ever lacks a `severity` (which would be the
    other way this could go wrong).
    """
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (int(time.time()), player_id),
    )
    conn.commit()
    conn.close()
    _seed_record(db_path, kind="ip_hash", value="shared-hash", former_player_id=42)

    resp = client.get("/api/admin/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "attention" in body and "worth_a_look" in body
    assert not any("evasion" in a["kind"] for a in body["attention"])
    assert all("severity" in a for a in body["attention"])
    assert any(w["player_id"] == player_id for w in body["worth_a_look"])
    assert all("severity" not in w for w in body["worth_a_look"])


# ---- POST /api/admin/worth-a-look/dismiss ----------------------------------

def test_dismiss_persists_and_removes_the_item_from_a_fresh_overview_load(client, db_path):
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'shared-hash', 'unknown', 'meshmapper-dart', 1, '{}')",
        (int(time.time()), player_id),
    )
    conn.commit()
    conn.close()
    _seed_record(db_path, kind="ip_hash", value="shared-hash", former_player_id=42)

    before = client.get("/api/admin/overview").json()
    assert any(w["player_id"] == player_id for w in before["worth_a_look"])

    resp = client.post(
        "/api/admin/worth-a-look/dismiss",
        json={"player_id": player_id, "signal": "evasion_ip_hash"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"player_id": player_id, "signal": "evasion_ip_hash", "dismissed": True}

    assert _count(db_path, "admin_worth_a_look_dismissal", "player_id", player_id) == 1

    after = client.get("/api/admin/overview").json()
    assert not any(w["player_id"] == player_id for w in after["worth_a_look"])


def test_dismiss_requires_no_confirmation_and_writes_no_admin_action_log(client, db_path):
    """Deliberately the one write route with no confirmation gate and
    no audit row -- see the route's own docstring for why."""
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path)

    before_log_count = _row(db_path, "SELECT COUNT(*) AS n FROM admin_action_log")["n"]
    resp = client.post(
        "/api/admin/worth-a-look/dismiss",
        json={"player_id": player_id, "signal": "evasion_ip_hash"},
    )
    assert resp.status_code == 200
    after_log_count = _row(db_path, "SELECT COUNT(*) AS n FROM admin_action_log")["n"]
    assert after_log_count == before_log_count


def test_dismiss_requires_a_role(db_path):
    app = FastAPI()
    app.include_router(admin_ops_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)
    resp = client.post("/api/admin/worth-a-look/dismiss", json={"player_id": 1, "signal": "evasion_ip_hash"})
    assert resp.status_code in (401, 404)


# ---- GET /api/admin/evasion-records ---------------------------------------

def test_list_returns_shape_without_value(client, db_path):
    _login_as(client, db_path, role="admin")
    _seed_record(db_path, kind="ip_hash", value="secret-hash-should-never-appear", former_player_id=7, reason="test reason")

    resp = client.get("/api/admin/evasion-records")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    entry = body[0]
    assert set(entry.keys()) == {"id", "former_player_id", "kind", "reason", "recorded_at"}
    assert entry["former_player_id"] == 7
    assert entry["kind"] == "ip_hash"
    assert entry["reason"] == "test reason"
    # The hard requirement: never the raw value.
    assert "secret-hash-should-never-appear" not in resp.text


def test_list_requires_a_role(db_path):
    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)
    resp = client.get("/api/admin/evasion-records")
    assert resp.status_code in (401, 404)


# ---- POST /api/admin/evasion-records/delete --------------------------------

def test_delete_removes_the_row_and_logs_admin_action(client, db_path):
    actor_id = _login_as(client, db_path, role="admin")
    record_id = _seed_record(db_path, kind="node_ref", value="mc:deadbeef", former_player_id=7)

    resp = client.post("/api/admin/evasion-records/delete", json={"id": record_id})
    assert resp.status_code == 200, resp.text

    assert _row(db_path, "SELECT 1 FROM mc_evasion_record WHERE id = ?", (record_id,)) is None

    log_row = _row(
        db_path,
        "SELECT actor_account_id, action, detail FROM admin_action_log "
        "WHERE action = 'evasion_record_delete' ORDER BY log_id DESC LIMIT 1",
    )
    assert log_row is not None
    assert log_row["actor_account_id"] == actor_id
    assert str(record_id) in log_row["detail"]
    assert "7" in log_row["detail"]


def test_delete_nonexistent_record_404s(client, db_path):
    _login_as(client, db_path, role="admin")
    resp = client.post("/api/admin/evasion-records/delete", json={"id": 999999})
    assert resp.status_code == 404


def test_delete_requires_a_role(db_path):
    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)
    resp = client.post("/api/admin/evasion-records/delete", json={"id": 1})
    assert resp.status_code in (401, 404)


# ---- POST /api/admin/player/mark-evasion / unmark-evasion ------------------

def test_mark_evasion_sets_column_and_logs_admin_action(client, db_path):
    actor_id = _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path)

    resp = client.post("/api/admin/player/mark-evasion", json={"player_id": player_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["evasion_marked"] is True
    assert body["evasion_marked_at"] is not None

    row = _row(db_path, "SELECT evasion_marked_at, disabled_at FROM player WHERE player_id = ?", (player_id,))
    assert row["evasion_marked_at"] is not None
    # Deliberately separate from disable -- marking must never disable.
    assert row["disabled_at"] is None

    log_row = _row(
        db_path,
        "SELECT actor_account_id FROM admin_action_log "
        "WHERE action = 'player_mark_evasion' ORDER BY log_id DESC LIMIT 1",
    )
    assert log_row is not None
    assert log_row["actor_account_id"] == actor_id


def test_unmark_evasion_clears_column_and_logs_admin_action(client, db_path):
    actor_id = _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path)
    client.post("/api/admin/player/mark-evasion", json={"player_id": player_id})

    resp = client.post("/api/admin/player/unmark-evasion", json={"player_id": player_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["evasion_marked"] is False

    row = _row(db_path, "SELECT evasion_marked_at FROM player WHERE player_id = ?", (player_id,))
    assert row["evasion_marked_at"] is None

    log_row = _row(
        db_path,
        "SELECT actor_account_id FROM admin_action_log "
        "WHERE action = 'player_unmark_evasion' ORDER BY log_id DESC LIMIT 1",
    )
    assert log_row is not None
    assert log_row["actor_account_id"] == actor_id


def test_mark_evasion_player_not_found_404s(client, db_path):
    _login_as(client, db_path, role="admin")
    resp = client.post("/api/admin/player/mark-evasion", json={"player_id": 999999})
    assert resp.status_code == 404


def test_mark_evasion_does_not_touch_ingestor_cache_or_send_a_notice(client, db_path):
    """Unlike disable/enable, marking must not invalidate the ingest
    key-auth cache (nothing about whether this player can play has
    changed) -- see _set_player_evasion_marked()'s own docstring."""
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path)

    resp = client.post("/api/admin/player/mark-evasion", json={"player_id": player_id})
    assert resp.status_code == 200

    ingestor = client.app.state.mc_ingestor
    assert player_id not in ingestor.invalidated
