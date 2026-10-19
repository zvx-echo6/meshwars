"""Player requests to group neighbouring regions: validation, the 3-a-day
limit, signed-out refusal, the operator mail (mocked), and approve /
decline in the admin API. Nothing is sent over a network."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app import email_login, region_requests, regions
from app.admin_api import router as admin_api_router
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.region_requests import router as requests_router
from app.regions_api import router as regions_router
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())
FIX = Path(__file__).parent / "fixtures" / "regions"


def _parsed(code: str) -> dict:
    """A stored-region fixture (tests/fixtures/regions/<CODE>.json) as the dict regions.store_region takes."""
    d = json.loads((FIX / f"{code.upper()}.json").read_text(encoding="utf-8"))
    d["rings"] = [[tuple(p) for p in ring] for ring in d["rings"]]
    return d


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
    c = sqlite3.connect(path, isolation_level=None)
    for code in ("boi", "pih", "ida", "smn"):
        parsed = _parsed(code)
        regions.store_region(c, code, parsed["name"], parsed, now=1000)
    c.close()
    return path


@pytest.fixture
def mails(monkeypatch):
    sent = []

    async def fake(to, **kw):
        sent.append((to, kw))

    monkeypatch.setattr(email_login, "send_region_request_email", fake)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.invalid")
    monkeypatch.setattr(settings, "regions_request_email", "admin@meshwars.com")
    return sent


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api_router)
    app.include_router(regions_router)
    app.include_router(requests_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return app


def _signed_in(path: str, *, with_player=True, name="ann", email="ann@example.com") -> TestClient:
    client = TestClient(_app())
    conn = sqlite3.connect(path)
    aid = conn.execute("INSERT INTO account(created_at, contact_email) VALUES (?, ?)", (NOW, email)).lastrowid
    if with_player:
        conn.execute("INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, 'RED', ?, ?)",
                     (name, NOW, aid))
    conn.commit()
    conn.close()
    client.cookies.set(SESSION_COOKIE_NAME, asyncio.run(create_session(aid, device_label=None)))
    return client


@pytest.fixture
def player(db_path, mails):
    return _signed_in(db_path)


@pytest.fixture
def admin(db_path, mails):
    client = TestClient(_app())
    conn = sqlite3.connect(db_path)
    aid = conn.execute("INSERT INTO account(created_at, role) VALUES (?, 'admin')", (NOW,)).lastrowid
    conn.execute("INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
                 "VALUES (?, 'unused', ?, ?)", (aid, NOW, NOW))
    conn.commit()
    conn.close()
    client.cookies.set(SESSION_COOKIE_NAME, asyncio.run(create_session(aid, device_label=None)))
    return client


GOOD = {"group_name": "Treasure Valley", "regions": ["BOI", "pih"], "note": "they share a border"}


def test_signed_out_is_refused(db_path, mails):
    r = TestClient(_app()).post("/api/regions/join-request", json=GOOD)
    assert r.status_code == 401 and mails == []


def test_account_without_a_player_is_refused(db_path, mails):
    r = _signed_in(db_path, with_player=False).post("/api/regions/join-request", json=GOOD)
    assert r.status_code == 403


@pytest.mark.parametrize("body,fragment", [
    ({"regions": ["BOI", "PIH"]}, "Name the group"),
    ({"group_name": "  ", "regions": ["BOI", "PIH"]}, "Name the group"),
    ({"group_name": "x" * 81, "regions": ["BOI", "PIH"]}, "at most 80"),
    ({"group_name": "G", "regions": ["BOI"]}, "at least 2"),
    ({"group_name": "G", "regions": ["BOI", "boi"]}, "at least 2 different"),
    ({"group_name": "G", "regions": "BOI,PIH"}, "at least 2"),
    ({"group_name": "G", "regions": ["BOI", 5]}, "at least 2"),
    ({"group_name": "G", "regions": ["BOI", "NOPE"]}, "Unknown region: NOPE"),
    ({"group_name": "G", "regions": ["BOI", "PIH"], "note": "n" * 1001}, "at most 1000"),
    ({"group_name": "G", "regions": ["BOI", "PIH"], "note": 5}, "Bad note"),
])
def test_validation(player, mails, body, fragment):
    r = player.post("/api/regions/join-request", json=body)
    assert r.status_code == 400 and fragment in r.json()["error"]
    assert mails == []


def test_a_good_request_is_stored_and_mailed(player, mails, db_path):
    r = player.post("/api/regions/join-request", json=GOOD)
    assert r.status_code == 201
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM region_join_request").fetchone()
    assert row["group_name"] == "Treasure Valley" and row["status"] == "pending"
    assert json.loads(row["region_codes"]) == ["BOI", "PIH"]
    c.close()
    assert len(mails) == 1
    to, kw = mails[0]
    assert to == "admin@meshwars.com"
    assert kw["subject"] == "Region group request: Treasure Valley"
    for needle in ("Group name: Treasure Valley", "BOI Boise, US", "PIH Pocatello, US", "ann (RED)",
                   "ann@example.com", "they share a border", "Submitted: ", "/admin#regions"):
        assert needle in kw["text"], needle
    assert kw["cta_url"].endswith("/admin#regions")
    assert "<td" in kw["details_html"] and "Treasure Valley" in kw["details_html"]


def test_mail_content_is_escaped_in_html(player, mails):
    player.post("/api/regions/join-request", json=dict(GOOD, group_name="<b>x</b>", note="a<script>"))
    html = mails[0][1]["details_html"]
    assert "<b>x</b>" not in html and "&lt;b&gt;" in html and "<script>" not in html


def test_boise_time_is_used():
    from datetime import datetime, timezone
    # 2026-10-12 18:00 UTC is 12:00 PM in Boise (MDT); in January it is MST
    summer = int(datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc).timestamp())
    winter = int(datetime(2026, 1, 12, 18, 0, tzinfo=timezone.utc).timestamp())
    assert "12:00 PM MDT" in region_requests._boise_time(summer)
    assert "11:00 AM MST" in region_requests._boise_time(winter)


def test_mail_failure_never_fails_the_request(player, monkeypatch, db_path):
    async def boom(*a, **k):
        raise email_login.EmailSendError("smtp down")

    monkeypatch.setattr(email_login, "send_region_request_email", boom)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.invalid")
    assert player.post("/api/regions/join-request", json=GOOD).status_code == 201
    c = sqlite3.connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM region_join_request").fetchone()[0] == 1
    c.close()


def test_unconfigured_mail_still_stores(player, mails, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    assert player.post("/api/regions/join-request", json=GOOD).status_code == 201
    assert mails == []


def test_three_a_day_then_429_and_it_rolls(player, mails, db_path):
    for _ in range(3):
        assert player.post("/api/regions/join-request", json=GOOD).status_code == 201
    r = player.post("/api/regions/join-request", json=GOOD)
    assert r.status_code == 429 and r.json() == {"error": "You can send 3 requests a day."}
    assert len(mails) == 3
    # another player is unaffected
    other = _signed_in(db_path, name="bob", email="bob@example.com")
    assert other.post("/api/regions/join-request", json=GOOD).status_code == 201
    # requests older than 24 h stop counting
    c = sqlite3.connect(db_path)
    c.execute("UPDATE region_join_request SET created_at = created_at - 90000 WHERE player_id = 1")
    c.commit()
    c.close()
    assert player.post("/api/regions/join-request", json=GOOD).status_code == 201


def test_invalid_request_does_not_use_up_the_limit(player, mails):
    for _ in range(5):
        assert player.post("/api/regions/join-request", json={"group_name": "G", "regions": ["BOI"]}).status_code == 400
    assert player.post("/api/regions/join-request", json=GOOD).status_code == 201


# ---- admin ----------------------------------------------------------------


def _make(player, **over):
    assert player.post("/api/regions/join-request", json=dict(GOOD, **over)).status_code == 201


def test_admin_lists_pending_first_with_every_detail(player, admin, mails):
    _make(player, group_name="First")
    _make(player, group_name="Second")
    first = admin.get("/api/admin/regions/requests").json()
    assert first["pending"] == 2
    assert [r["group_name"] for r in first["requests"]] == ["First", "Second"]
    r0 = first["requests"][0]
    assert r0["player"] == "ann" and r0["team"] == "RED" and r0["email"] == "ann@example.com"
    assert r0["regions"] == [{"code": "BOI", "name": "Boise, US"}, {"code": "PIH", "name": "Pocatello, US"}]
    assert r0["note"] == "they share a border" and r0["created_at"]
    admin.post("/api/admin/regions/requests/decline", json={"id": r0["id"]})
    after = admin.get("/api/admin/regions/requests").json()
    assert after["pending"] == 1
    assert [r["group_name"] for r in after["requests"]] == ["Second", "First"]   # pending first


def test_approve_creates_the_group_and_records_who_and_when(player, admin, mails, db_path):
    _make(player)
    rid = admin.get("/api/admin/regions/requests").json()["requests"][0]["id"]
    r = admin.post("/api/admin/regions/requests/approve", json={"id": rid})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    groups = admin.get("/api/admin/regions").json()["groups"]
    assert [(g["name"], g["slug"], g["regions"]) for g in groups] == [("Treasure Valley", "treasure-valley", ["BOI", "PIH"])]
    done = admin.get("/api/admin/regions/requests").json()["requests"][0]
    assert done["status"] == "approved" and done["decided_at"] and done["group_id"] == groups[0]["id"]
    assert done["decided_by"] is not None
    # a decided request cannot be decided again
    assert admin.post("/api/admin/regions/requests/approve", json={"id": rid}).status_code == 409
    assert admin.post("/api/admin/regions/requests/decline", json={"id": rid}).status_code == 409


def test_decline_marks_it_and_creates_nothing(player, admin, mails):
    _make(player)
    rid = admin.get("/api/admin/regions/requests").json()["requests"][0]["id"]
    assert admin.post("/api/admin/regions/requests/decline", json={"id": rid}).json()["status"] == "declined"
    assert admin.get("/api/admin/regions").json()["groups"] == []
    assert admin.get("/api/admin/regions/requests").json()["pending"] == 0


def test_slug_collision_leaves_it_pending_and_a_new_name_works(player, admin, mails):
    admin.post("/api/admin/regions/groups/create", json={"name": "Treasure Valley", "regions": ["SMN"]})
    _make(player)
    rid = admin.get("/api/admin/regions/requests").json()["requests"][0]["id"]
    r = admin.post("/api/admin/regions/requests/approve", json={"id": rid})
    assert r.status_code == 409 and "slug" in r.json()["error"]
    assert admin.get("/api/admin/regions/requests").json()["pending"] == 1
    ok = admin.post("/api/admin/regions/requests/approve", json={"id": rid, "group_name": "Treasure Valley East"})
    assert ok.status_code == 200
    slugs = {g["slug"] for g in admin.get("/api/admin/regions").json()["groups"]}
    assert slugs == {"treasure-valley", "treasure-valley-east"}


def test_admin_request_routes_need_a_role(db_path, mails):
    anon = TestClient(_app())
    assert anon.get("/api/admin/regions/requests").status_code in (401, 404)
    assert anon.post("/api/admin/regions/requests/approve", json={"id": 1}).status_code in (401, 404)
    assert anon.post("/api/admin/regions/requests/decline", json={"id": 1}).status_code in (401, 404)


def test_unknown_request_is_404(admin):
    assert admin.post("/api/admin/regions/requests/approve", json={"id": 999}).status_code == 404
    assert admin.post("/api/admin/regions/requests/decline", json={"id": 999}).status_code == 404
    assert admin.post("/api/admin/regions/requests/decline", json={}).status_code == 400


def test_the_email_fragment_renders_in_the_shell():
    html = email_login._render_html(email_login.PURPOSE_REGION_REQUEST, "logo-cid",
                                    details_html="<b>DETAILS</b>", cta_url="https://x.invalid/admin#regions")
    assert "Region group request" in html and "<b>DETAILS</b>" in html
    # Three, by design and identical to sign_in.html / security_notice.html: the button's href, the
    # plain-text fallback link's href, and that link's visible text.
    assert html.count("https://x.invalid/admin#regions") == 3
    assert "{details_html}" not in html and "{cta_url}" not in html and "{logo_cid}" not in html
