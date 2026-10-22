"""Caching rules for the static UI.

The appliance is updated in place and the service restarts, so a browser that keeps
serving the previous app.js/style.css from its cache is a real failure mode. These
tests pin the contract: the document is never stored, referenced assets carry a stamp
that changes with the file, and only stamped URLs may be cached long-term.
"""

import re

from fastapi.testclient import TestClient

from app import __version__
from app.main import _asset_stamp, app

# No ``with`` block on purpose: that would run the lifespan and start the engine.
client = TestClient(app)


def test_index_is_never_stored():
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


def test_index_stamps_every_local_script_and_stylesheet():
    body = client.get("/").text
    for asset in ("/app.js", "/style.css", "/i18n.js", "/i18n/en.js", "/i18n/de.js",
                  "/i18n/it.js"):
        assert re.search(re.escape(asset) + r"\?v=[0-9a-f]+", body), asset
    # Plain links must stay untouched - the manuals are served with revalidation.
    assert '"/manual.html?v=' not in body


def test_stamped_assets_may_be_cached_forever():
    r = client.get("/app.js?v=deadbeef")
    assert r.status_code == 200
    assert "immutable" in r.headers["cache-control"]


def test_unstamped_assets_must_be_revalidated():
    for path in ("/app.js", "/style.css", "/manual.html", "/handbuch.html"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["cache-control"] == "no-cache", path
        assert r.headers.get("etag"), path  # revalidation only pays off with one


def test_asset_stamp_follows_the_file(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "WEB_DIR", tmp_path)
    f = tmp_path / "app.js"
    f.write_text("x", encoding="utf-8")

    first = main._asset_stamp("/app.js")
    assert first == main._asset_stamp("/app.js")  # stable while the file is
    f.write_text("xy", encoding="utf-8")
    assert main._asset_stamp("/app.js") != first


def test_asset_stamp_falls_back_instead_of_raising(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "WEB_DIR", tmp_path)
    assert main._asset_stamp("/gone.js") == __version__


def test_asset_stamp_of_a_real_asset_is_hex():
    assert re.fullmatch(r"[0-9a-f]+", _asset_stamp("/app.js"))
