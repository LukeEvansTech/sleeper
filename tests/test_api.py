"""The New view and the JSON a phone app reads, against a swept database."""

import pytest
from fastapi.testclient import TestClient
from sleeper import config, sweep
from sleeper.db import DB
from sleeper.web import create_app

ROW = {
    "id": "111",
    "title": "PSA 10 Vegito BT7-118 SPR Dragon Ball Super",
    "price": "$700.00",
    "text": "PSA 10 Vegito BT7-118 SPR $700.00 +$5.64 delivery 3 bids 35m left",
    "img": "https://i.ebayimg.com/images/g/x/s-l140.jpg",
}
BIN = {**ROW, "text": "PSA 10 Vegito BT7-118 SPR $700.00 +$5.64 delivery"}  # buy it now


class FakeBrowser:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def search(self, url):
        return [] if "LH_Sold" in url else self.rows

    def item_photos(self, _item_id):
        return []


@pytest.fixture(name="env")
def fixture_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    monkeypatch.setenv("SLEEPER_PUBLIC_URL", "https://sleeper.example")
    s = config.load()
    db = DB(s.db_path)
    return s, db, TestClient(create_app(s, db))


def test_new_view_and_json_api(env, monkeypatch):
    """The New view lists deals first seen in the last day, and /api/v1 gives a phone app the same
    listings, deal page and wants as the pages."""
    s, db, client = env
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    client.post(
        "/api/import",
        json={
            "wants": [
                {
                    "set": "DBS",
                    "who": "Vegito",
                    "q": "vegito psa 10",
                    "max": {"any": 1150},
                }
            ],
            "pop": {"fetched": "2026-11-04", "rows": []},
        },
    )
    old = {**BIN, "id": "333", "price": "$640.00"}
    sweep.run(db, s, browser=lambda: FakeBrowser([BIN, old, ROW]))
    db.run("UPDATE listings SET first_seen = '2020-01-01T00:00:00' WHERE item_id = '333'")

    every = client.get("/api/v1/deals").json()
    assert every["show"] == "deals" and every["sort"] == "deal" and every["new_hours"] == 24
    ids = {d["item_id"]: d for d in every["deals"]}
    assert ids["111"]["new"] is True and ids["333"]["new"] is False
    assert ids["333"]["total"] == pytest.approx(645.64) and ids["333"]["ceiling"] == 1150
    assert ids["333"]["ebay_url"] == "https://www.ebay.com/itm/333"
    assert ids["333"]["gixen_url"] is None  # buy it now: nothing to snipe
    assert ids["333"]["first_seen"].startswith("2020-01-01T00:00:00")
    assert len(ids["333"]["first_seen"]) > 19  # carries its UTC offset

    new = client.get("/api/v1/deals?show=new").json()
    assert new["sort"] == "new" and [d["item_id"] for d in new["deals"]] == ["111"]
    page = client.get("/?show=new&sort=new")
    assert "1 new deal<" in page.text and "/deal/333/psa" not in page.text
    assert 'href="?show=new&amp;sort=new"' in client.get("/").text
    assert "/static/app.css?v=" in page.text and "/static/app.js?v=" in page.text
    assert client.get("/api/v1/deals?show=nonsense").json()["show"] == "all"

    one = client.get("/api/v1/deals/333/psa").json()
    assert one["reasons"] and one["links"][0]["url"].startswith("https://www.ebay.com/")
    assert [o["item_id"] for o in one["others"]] and one["photo"].startswith("https://")
    assert client.get("/api/v1/deals/999/psa").status_code == 404

    wants = client.get("/api/v1/wants").json()
    assert wants["total"] == 1 and wants["sets"][0]["name"] == "DBS"
    assert wants["sets"][0]["wants"][0]["name"] == "Vegito"
    assert wants["sets"][0]["wants"][0]["deals"] >= 1
    assert client.get("/api/v1/ended").json() == {
        "total": 0,
        "sold": 0,
        "tally": {"open": 0, "range": 0, "lost": 0},
        "deals": [],
    }

    assert client.post("/api/v1/deals/999/psa/block", json={}).status_code == 404
    assert client.post("/api/v1/deals/333/psa/block", json={"reason": "damaged"}).json() == {
        "blocked": "333"
    }
    assert "333" not in {d["item_id"] for d in client.get("/api/v1/deals").json()["deals"]}
    assert db.one("SELECT reason FROM blocked WHERE item_id = '333'")["reason"] == "damaged"
