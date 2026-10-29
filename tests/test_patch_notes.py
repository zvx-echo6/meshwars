"""/patch-notes renders CHANGELOG.md; the renderer escapes everything."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import patch_notes
from app.api import _templated_html_page


def _client() -> TestClient:
    """The real route lives inside create-time wiring in app/api.py, so
    build the same page through the same helper the route uses."""
    from pathlib import Path

    app = FastAPI()
    front = Path(__file__).resolve().parent.parent / "frontend"

    @app.get("/patch-notes")
    async def page(request):
        return _templated_html_page(
            request, front / "patch-notes.html", "missing",
            substitutions={"<!--PATCH_NOTES-->": patch_notes.rendered_changelog()},
        )

    return TestClient(app)


def test_patch_notes_page_has_known_heading():
    resp = _client().get("/patch-notes")
    assert resp.status_code == 200
    assert "2026-10-29" in resp.text
    assert "Deferred pings now claim empty squares" in resp.text
    assert "<!--PATCH_NOTES-->" not in resp.text
    assert "Patch notes</a>" in resp.text  # footer link


def test_real_app_serves_patch_notes():
    from app.main import app
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/patch-notes" in paths


def test_html_in_markdown_is_escaped():
    out = patch_notes.render_markdown(
        "## <script>alert(1)</script>\n\n"
        "Text <img src=x onerror=alert(1)> and **bold** and `<b>`.\n\n"
        "- item <i>one</i>\n"
    )
    assert "<script>" not in out and "<img" not in out and "<i>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>bold</strong>" in out
    assert "<code>&lt;b&gt;</code>" in out
    assert "<li>item &lt;i&gt;one&lt;/i&gt;</li>" in out


def test_only_http_and_site_links_become_anchors():
    out = patch_notes.render_markdown(
        "[ok](/privacy) [ext](https://example.com/a?b=1&c=2) "
        "[bad](javascript:alert(1)) [proto](//evil.example)"
    )
    assert '<a href="/privacy">ok</a>' in out
    assert '<a href="https://example.com/a?b=1&amp;c=2">ext</a>' in out
    assert "href=\"javascript" not in out
    assert 'href="//evil' not in out
