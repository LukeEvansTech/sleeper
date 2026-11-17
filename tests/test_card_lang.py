"""The raw-card language check: a Japanese print titled with no language is not the English card."""

import pytest
from fastapi.testclient import TestClient
from sleeper import config, slab, sweep
from sleeper.db import DB
from sleeper.web import create_app
from test_web import FakeBrowser

RAW = {
    "id": "444",
    "title": "Union Arena Fullmetal Alchemist Envy SR Parallel 2 Star FMA-1-077",
    "price": "$75.00",
    "text": "Union Arena Envy FMA-1-077 $75.00 +$5.00 delivery",
    "img": "https://i.ebayimg.com/images/g/x/s-l140.jpg",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    s = config.load()
    db = DB(s.db_path)
    client = TestClient(create_app(s, db))
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    want = {"set": "DBS", "who": "Envy", "q": "envy psa 10", "rawq": "envy", "nostar": True}
    client.post("/api/import", json={"wants": [{**want, "max": {"any": 300}, "raw_max": 100}]})
    sweep.run(db, s, browser=lambda: FakeBrowser([RAW]))
    s = s.__class__(**{**s.__dict__, "llm_url": "http://llm.invalid"})
    monkeypatch.setattr(sweep.photos, "download", lambda _src, dest: dest)
    db.run("UPDATE listings SET deal = 1 WHERE kind = 'raw'")
    return s, db


def run_id(db):
    return db.one("SELECT max(id) AS n FROM runs")["n"]


def test_a_japanese_raw_card_for_an_english_want_is_dropped(env, monkeypatch):
    s, db = env
    monkeypatch.setattr(
        sweep.slab, "read_card_lang", lambda *a: {"card_lang": "JAPANESE", "sure": True}
    )
    assert sweep.lang_check(db, s, run_id(db)) == 1
    row = db.one("SELECT active, deal FROM listings WHERE item_id = '444' AND kind = 'raw'")
    assert (row["active"], row["deal"]) == (1, 0)  # held off deals, not blocked for every want
    db.run("UPDATE listings SET deal = 1 WHERE kind = 'raw'")  # next sweep's rating
    monkeypatch.setattr(sweep.slab, "read_card_lang", lambda *a: pytest.fail("read again"))
    assert sweep.lang_check(db, s, run_id(db)) == 1


def test_an_english_card_stays_and_is_read_once(env, monkeypatch):
    s, db = env
    monkeypatch.setattr(
        sweep.slab, "read_card_lang", lambda *a: {"card_lang": "ENGLISH", "sure": True}
    )
    assert sweep.lang_check(db, s, run_id(db)) == 0
    monkeypatch.setattr(sweep.slab, "read_card_lang", lambda *a: pytest.fail("read again"))
    assert sweep.lang_check(db, s, run_id(db)) == 0
    assert db.one("SELECT deal FROM listings WHERE item_id = '444' AND kind = 'raw'")["deal"] == 1


def test_an_unread_deal_over_the_limit_is_held(env, monkeypatch):
    s, db = env
    monkeypatch.setattr(sweep, "MAX_LANG_READS", 0)
    sweep.lang_check(db, s, run_id(db))
    assert db.one("SELECT deal FROM listings WHERE item_id = '444' AND kind = 'raw'")["deal"] == 0


def test_card_language_verdicts():
    jp = {"card_lang": "JAPANESE", "sure": True}
    assert slab.card_wrong(jp) == "card is Japanese"
    assert slab.card_wrong(jp, "japanese") == ""
    assert slab.card_wrong({"card_lang": "JAPANESE", "sure": False}) == ""
    assert slab.card_wrong({"card_lang": "NONE", "sure": True}) == ""
    assert slab.card_wrong({"card_lang": "ENGLISH", "sure": True}, "chinese") == "card is English"


def test_a_failed_read_is_noted_and_retried_after_unread_listings(env, monkeypatch):
    s, db = env
    monkeypatch.setattr(sweep.slab, "read_card_lang", lambda *a: None)
    sweep.lang_check(db, s, run_id(db))
    assert "card_failed" in db.one("SELECT slab FROM listings WHERE kind = 'raw'")["slab"]
    db.run("UPDATE listings SET deal = 1 WHERE kind = 'raw'")
    monkeypatch.setattr(
        sweep.slab, "read_card_lang", lambda *a: {"card_lang": "ENGLISH", "sure": True}
    )
    assert sweep.lang_check(db, s, run_id(db)) == 0
    assert "card_lang" in db.one("SELECT slab FROM listings WHERE kind = 'raw'")["slab"]


def test_a_failed_read_over_the_limit_is_not_held(env, monkeypatch):
    s, db = env
    db.run(
        "UPDATE listings SET slab = ? WHERE kind = 'raw'",
        '{"card_failed": "2026-11-17T00:00:00", "photo": ""}',
    )
    monkeypatch.setattr(sweep, "MAX_LANG_READS", 0)
    sweep.lang_check(db, s, run_id(db))
    assert db.one("SELECT deal FROM listings WHERE item_id = '444' AND kind = 'raw'")["deal"] == 1


def test_a_known_wrong_language_stays_held_when_a_reread_fails(env, monkeypatch):
    s, db = env
    db.run(
        "UPDATE listings SET slab = ? WHERE kind = 'raw'",
        '{"card_lang": "JAPANESE", "sure": true, "photo": "old.jpg"}',
    )
    monkeypatch.setattr(sweep.slab, "read_card_lang", lambda *a: None)
    assert sweep.lang_check(db, s, run_id(db)) == 1
    assert db.one("SELECT deal FROM listings WHERE item_id = '444' AND kind = 'raw'")["deal"] == 0


def test_an_inconclusive_reread_keeps_the_earlier_verdict(env, monkeypatch):
    s, db = env
    db.run(
        "UPDATE listings SET slab = ? WHERE kind = 'raw'",
        '{"card_lang": "JAPANESE", "sure": true, "photo": "old.jpg"}',
    )
    monkeypatch.setattr(
        sweep.slab, "read_card_lang", lambda *a: {"card_lang": "NONE", "sure": False}
    )
    assert sweep.lang_check(db, s, run_id(db)) == 1
    assert "JAPANESE" in db.one("SELECT slab FROM listings WHERE kind = 'raw'")["slab"]
