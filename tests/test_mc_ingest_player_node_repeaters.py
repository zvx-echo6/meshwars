"""A registered mc player radio is never a repeater (app/mc_ingest.py
drop_player_node_repeaters), plus the owner ruling: one player gains
nothing from two contacts / two API keys."""
from __future__ import annotations

import sqlite3
import time

import pytest

from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_id as grid_cell_id
from app.mc_ingest import McIngestor, RepeaterEntry, _invalidate_player_node_cache, is_player_node_id
from app import mc_scoring

NOW = int(time.time())
LAT, LON = 43.0, -116.0
CELL = grid_cell_id(LAT, LON)


@pytest.fixture(autouse=True)
def _clear_cache():
    _invalidate_player_node_cache()
    yield
    _invalidate_player_node_cache()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "game.db"
    monkeypatch.setattr(settings, "db_path", str(path))
    c = sqlite3.connect(str(path))
    c.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            c.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    c.commit()
    c.close()
    return str(path)


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, args)]
    c.close()
    return rows


def _player(db_path, pid, team="RED"):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)",
              (pid, f"p{pid}", team, NOW))
    c.commit(); c.close()


def _node(db_path, pid, ref, pub=None):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO player_node(protocol, node_ref, player_id, bound_at, public_key) "
              "VALUES ('mc',?,?,?,?)", (ref, pid, NOW, pub))
    c.commit(); c.close()


def _season(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("BEGIN IMMEDIATE")
    mc_scoring.maybe_roll_season(c, NOW, "mc")
    sid = mc_scoring.ensure_active_season(c, NOW, "mc")
    c.execute("COMMIT"); c.close()
    return sid


def _ping(contact, repeaters, ts=NOW):
    return {"type": "RX", "contact": contact, "lat": LAT, "lon": LON, "timestamp": ts,
            "heard_repeats": ",".join(f"{r}(3.5)" for r in repeaters)}


def _run(pid, ping, key="k1", received=NOW):
    McIngestor()._process_batch_sync(pid, key, [ping], received)


def _obs_ids(db_path):
    return {r["repeater_id"] for r in _q(db_path, "SELECT repeater_id FROM repeater_observation")}


def _credits(db_path):
    return {r["repeater_id"] for r in _q(db_path, "SELECT repeater_id FROM player_cell_repeater_credit")}


def test_own_node_as_repeater_no_credit_no_observation(db_path):
    _player(db_path, 1); _season(db_path)
    _run(1, _ping("deadbeef", ["dead", "cafefeed"]))
    assert _obs_ids(db_path) == {"cafefeed"}
    assert _credits(db_path) == {"cafefeed"}


def test_own_node_only_behaves_like_no_repeaters(db_path):
    _player(db_path, 1); _season(db_path)
    _run(1, _ping("deadbeef", ["deadbeef"]))
    assert _obs_ids(db_path) == set()
    assert _q(db_path, "SELECT * FROM mc_tile_score") == []
    stat = _q(db_path, "SELECT SUM(pings_accepted) a, SUM(pings_no_repeaters) n FROM player_ingest_stat")[0]
    assert stat["a"] == 1 and stat["n"] == 1


def test_teammate_node_no_credit(db_path):
    _player(db_path, 1); _player(db_path, 2); _season(db_path)
    _node(db_path, 2, "a1b2c3d4")
    _run(1, _ping("deadbeef", ["a1b2", "cafefeed"]))
    assert _obs_ids(db_path) == {"cafefeed"}


def test_public_key_prefix_matches(db_path):
    _player(db_path, 1); _player(db_path, 2); _season(db_path)
    pub = "ab12" + "0" * 60
    _node(db_path, 2, "11223344", pub)
    _run(1, _ping("deadbeef", ["ab12", "cafefeed"]))
    assert _obs_ids(db_path) == {"cafefeed"}


def test_real_repeater_credited(db_path):
    _player(db_path, 1); _player(db_path, 2); _season(db_path)
    _node(db_path, 2, "a1b2c3d4")
    _run(1, _ping("deadbeef", ["cafefeed"]))
    assert _credits(db_path) == {"cafefeed"}
    assert _obs_ids(db_path) == {"cafefeed"}


def test_one_byte_id_unchanged(db_path):
    _player(db_path, 1); _season(db_path)
    _run(1, _ping("deadbeef", ["de"]))
    assert _obs_ids(db_path) == {"de"}


def test_long_id_rule():
    nodes = [("a1b2c3d4", "a1b2c3d4" + "0" * 56), ("11223344", None)]
    assert is_player_node_id("a1b2c3d4", nodes)
    assert is_player_node_id("a1b2c3d4" + "00", nodes)         # key matches
    assert not is_player_node_id("a1b2c3d4" + "ff", nodes)      # key mismatch
    assert is_player_node_id("11223344ff", nodes)               # no key -> node_ref only
    assert not is_player_node_id("a1b", nodes)                  # < 4 hex


def test_two_contacts_two_keys_score_once(db_path):
    """Owner ruling: same player, two bound contacts / two API keys, same
    cell, same repeater inside the cooldown -> scored once, unique bonus once."""
    _player(db_path, 1); sid = _season(db_path)
    _run(1, _ping("deadbeef", ["cafefeed"], NOW), key="k1", received=NOW)
    _run(1, _ping("feedface", ["cafefeed"], NOW + 10), key="k2", received=NOW + 10)
    assert len(_q(db_path, "SELECT 1 FROM player_node WHERE player_id = 1")) == 2
    score = _q(db_path, "SELECT score FROM mc_tile_score WHERE team='RED'")[0]["score"]
    assert score == pytest.approx(settings.mc_points_per_repeater + settings.mc_score_per_unique_player, abs=0.01)
    assert len(_q(db_path, "SELECT 1 FROM mc_tile_unique_painter WHERE player_id = 1")) == 1
    assert len(_q(db_path, "SELECT 1 FROM player_cell_repeater_credit WHERE player_id = 1")) == 1
