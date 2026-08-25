"""Tests for app/api.py's tile_file() -- the hand-rolled byte-range
endpoint that replaced the /tiles StaticFiles mount (see the module
comment above tile_file() in app/api.py for why: the pinned
starlette==0.38.6 does not forward an incoming Range header through
StaticFiles at all).

Builds a minimal FastAPI app around tile_file() directly, rather than
calling app.api.mount() (which wires up every router in the app and
needs the full settings surface) -- monkeypatching settings.tiles_dir
to a tmp_path fixture and dropping a small fake .pmtiles file there is
enough to exercise the endpoint end to end over real HTTP via
TestClient, including actual Range header handling.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import tile_file
from app.config import settings

CONTENT = bytes(range(256)) * 4  # 1024 bytes, byte value == index % 256 -- easy to assert on


@pytest.fixture
def client(tmp_path, monkeypatch):
    tiles_dir = tmp_path / "tiles-data"
    tiles_dir.mkdir()
    (tiles_dir / "test.pmtiles").write_bytes(CONTENT)
    (tiles_dir / "notes.txt").write_bytes(b"not a tile archive")

    # A directory outside tiles_dir, standing in for something a
    # traversal attempt might try to reach (e.g. an app secret).
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "passwd").write_bytes(b"root:x:0:0::/root:/bin/bash\n")
    (secret_dir / "passwd.pmtiles").write_bytes(b"root:x:0:0::/root:/bin/bash\n")

    monkeypatch.setattr(settings, "tiles_dir", str(tiles_dir))

    app = FastAPI()
    app.add_api_route("/tiles/{filename:path}", tile_file, methods=["GET"])
    return TestClient(app)


def test_no_range_returns_200_with_full_body(client):
    resp = client.get("/tiles/test.pmtiles")
    assert resp.status_code == 200
    assert resp.content == CONTENT
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == str(len(CONTENT))
    assert "content-range" not in resp.headers


def test_closed_range_returns_206_with_correct_bytes_and_content_range(client):
    resp = client.get("/tiles/test.pmtiles", headers={"Range": "bytes=10-19"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[10:20]
    assert resp.headers["content-range"] == f"bytes 10-19/{len(CONTENT)}"
    assert resp.headers["content-length"] == "10"
    assert resp.headers["accept-ranges"] == "bytes"


def test_open_ended_range_returns_rest_of_file(client):
    resp = client.get("/tiles/test.pmtiles", headers={"Range": "bytes=1000-"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[1000:]
    assert resp.headers["content-range"] == f"bytes 1000-{len(CONTENT) - 1}/{len(CONTENT)}"
    assert resp.headers["content-length"] == str(len(CONTENT) - 1000)


def test_pmtiles_first_127_bytes_matches_the_real_curl_proof(client):
    """The exact check run against the deployed endpoint: PMTiles' own
    opening read is 127 bytes of header."""
    resp = client.get("/tiles/test.pmtiles", headers={"Range": "bytes=0-126"})
    assert resp.status_code == 206
    assert len(resp.content) == 127
    assert resp.content == CONTENT[0:127]
    assert resp.headers["content-range"] == f"bytes 0-126/{len(CONTENT)}"


def test_range_start_beyond_file_size_is_416(client):
    resp = client.get("/tiles/test.pmtiles", headers={"Range": f"bytes={len(CONTENT)}-{len(CONTENT) + 10}"})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(CONTENT)}"


def test_multi_range_request_is_416(client):
    resp = client.get("/tiles/test.pmtiles", headers={"Range": "bytes=0-9,20-29"})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(CONTENT)}"


def test_range_end_beyond_file_size_is_clamped_not_rejected(client):
    resp = client.get("/tiles/test.pmtiles", headers={"Range": "bytes=0-99999"})
    assert resp.status_code == 206
    assert resp.content == CONTENT
    assert resp.headers["content-range"] == f"bytes 0-{len(CONTENT) - 1}/{len(CONTENT)}"


def test_missing_file_is_404(client):
    resp = client.get("/tiles/nope.pmtiles")
    assert resp.status_code == 404


def test_non_pmtiles_file_is_refused_even_though_it_exists(client):
    resp = client.get("/tiles/notes.txt")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "../secret/passwd.pmtiles",
        "../../secret/passwd.pmtiles",
        "..%2Fsecret%2Fpasswd.pmtiles",
        "%2e%2e/secret/passwd.pmtiles",
    ],
)
def test_traversal_attempts_are_404_and_never_read_outside_tiles_dir(client, path):
    resp = client.get(f"/tiles/{path}")
    assert resp.status_code == 404
    assert b"root:x:0:0" not in resp.content


def test_absolute_path_traversal_is_404(client, tmp_path):
    """Path("/tiles-data") / "/secret/passwd.pmtiles" collapses to plain
    "/secret/passwd.pmtiles" in pathlib -- joining an absolute path onto
    another one discards what came before it -- so an absolute filename
    has to be rejected before that join ever happens, or this would walk
    straight past tiles_dir to the real file. Uses a target that DOES
    end in .pmtiles (unlike /etc/passwd) so the extension check alone
    can't be the thing making this pass -- only the absolute-path guard
    can be.
    """
    resp = client.get("/tiles//secret/passwd.pmtiles")
    assert resp.status_code == 404
    assert b"root:x:0:0" not in resp.content


def test_etag_present_and_if_none_match_returns_304(client):
    first = client.get("/tiles/test.pmtiles")
    etag = first.headers["etag"]
    assert etag

    second = client.get("/tiles/test.pmtiles", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""


def test_cache_control_is_long_and_immutable(client):
    resp = client.get("/tiles/test.pmtiles")
    cache_control = resp.headers["cache-control"]
    assert "immutable" in cache_control
    assert "max-age=31536000" in cache_control
