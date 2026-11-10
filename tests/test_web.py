"""End to end: import wants, sweep with a fake browser, open the alert's deal page."""

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sleeper import config, photos, sweep
from sleeper.db import DB
from sleeper.web import create_app


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


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    monkeypatch.setenv("SLEEPER_PUBLIC_URL", "https://sleeper.example")
    s = config.load()
    db = DB(s.db_path)
    return s, db, TestClient(create_app(s, db))


ROW = {
    "id": "111",
    "title": "PSA 10 Vegito BT7-118 SPR Dragon Ball Super",
    "price": "$700.00",
    "text": "PSA 10 Vegito BT7-118 SPR $700.00 +$5.64 delivery 3 bids 35m left",
    "img": "https://i.ebayimg.com/images/g/x/s-l140.jpg",
}


BIN = {**ROW, "text": "PSA 10 Vegito BT7-118 SPR $700.00 +$5.64 delivery"}  # buy it now


def test_import_sweep_and_deal_page(env, monkeypatch):
    s, db, client = env
    sent = []
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: sent.append(a) or "sent")
    r = client.post(
        "/api/import",
        json={
            "wants": [{"set": "DBS", "who": "Vegito", "q": "vegito psa 10", "max": {"any": 1150}}],
            "pop": {"fetched": "2026-11-04", "rows": []},
        },
    )
    assert r.json()["added"] == 1
    # first sweep only records a baseline: one summary alert, nothing per deal
    run1 = sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert db.one("SELECT status FROM runs WHERE id = ?", run1)["status"] == "success"
    assert len(sent) == 1 and "watching" in sent[0][1]
    # a new listing on the next sweep gets its own alert linking to its deal page
    row2 = {**ROW, "id": "222", "price": "$650.00"}
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW, row2]))
    assert len(sent) == 2
    assert sent[1][3] == "https://sleeper.example/deal/222/psa"
    page = client.get("/deal/222/psa")
    assert page.status_code == 200
    assert "Vegito" in page.text and "$656" in page.text
    assert "Max bid" in page.text and "<b>$1,144.36</b>" in page.text
    assert client.get("/").status_code == 200
    assert client.get(f"/run/{run1}").status_code == 200
    assert client.get("/wants").status_code == 200
    assert client.get("/ops").status_code == 200
    assert "sleeper_last_success_timestamp_seconds" in client.get("/metrics").text


def test_failed_send_is_retried_next_sweep(env, monkeypatch):
    s, db, client = env
    client.post("/api/import", json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v"}]})
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    sweep.run(db, s, browser=lambda: FakeBrowser([]))  # baseline with nothing live
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "failed: x")
    db.run("UPDATE wants SET spec = json_set(spec, '$.max', json('{\"any\": 900}'))")
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert db.one("SELECT count(*) AS n FROM alerts")["n"] == 0
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert db.one("SELECT count(*) AS n FROM alerts")["n"] == 1


def test_want_form_validates(env):
    _, _, client = env
    r = client.post("/wants", data={"who": "Cloud", "set": "FIN", "extra": "{}", "enabled": "on"})
    assert "need an eBay search" in r.text
    r = client.post(
        "/wants",
        data={"who": "Cloud", "set": "FIN", "q": "c", "max_any": "inf", "extra": "{}"},
    )
    assert "zero or more" in r.text
    r = client.post(
        "/wants",
        data={
            "who": "Cloud",
            "set": "FIN",
            "q": "cloud surge foil",
            "max_any": "$45",
            "extra": "{}",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get(r.headers["location"]).status_code == 200


def test_ref_upload_rejects_path_tricks(env):
    _, _, client = env
    assert client.put("/api/refs/..%2Fx.jpg", content=b"x" * 2000).status_code in (400, 404)
    assert client.put("/api/refs/ok.jpg", content=b"x" * 2000).status_code == 200
    assert client.get("/refs/ok.jpg").status_code == 200


def test_sold_import_maps_wants_and_feeds_verdict(env):
    _, db, client = env
    client.post("/api/import", json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v"}]})
    sold = {
        "9": {
            "set": "DBS",
            "who": "Vegito",
            "kind": "psa",
            "star": "-",
            "usd": 1188,
            "date": "2099-01-01",
            "title": "Vegito PSA 10",
        },
        "8": {
            "set": "DBS",
            "who": "Nobody",
            "kind": "psa",
            "star": "-",
            "usd": 5,
            "date": "2099-01-01",
            "title": "x",
        },
    }
    assert client.post("/api/sold", json={"sold": sold}).json() == {"added": 1}
    assert client.post("/api/sold", json={"sold": sold}).json() == {"added": 0}
    assert db.one("SELECT usd FROM sold")["usd"] == 1188


def test_failed_baseline_send_is_retried(env, monkeypatch):
    s, db, client = env
    client.post(
        "/api/import",
        json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 900}}]},
    )
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "failed: x")
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert db.one("SELECT count(*) AS n FROM alerts")["n"] == 0
    sent = []
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: sent.append(a) or "sent")
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert len(sent) == 1 and "watching" in sent[0][1]
    assert db.one("SELECT count(*) AS n FROM alerts")["n"] == 1


def test_sweep_with_no_wants_is_not_a_success(env):
    s, db, _ = env
    run_id = sweep.run(db, s, browser=lambda: FakeBrowser([]))
    assert db.one("SELECT status FROM runs WHERE id = ?", run_id)["status"] == "failed"
    assert not db.get("baseline_done", False)


def test_unticked_watching_pauses_and_delete_clears_sales(env):
    _, db, client = env
    r = client.post(
        "/wants",
        data={"who": "Cloud", "set": "FIN", "q": "cloud", "extra": "{}"},
        follow_redirects=False,
    )
    want_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert db.want(want_id)["_enabled"] is False
    client.post(
        "/api/sold",
        json={
            "sold": {
                "1": {
                    "set": "FIN",
                    "who": "Cloud",
                    "kind": "psa",
                    "star": "-",
                    "usd": 10,
                    "date": "2099-01-01",
                    "title": "t",
                }
            }
        },
    )
    client.post(f"/wants/{want_id}/delete")
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 0
    r = client.post(
        "/wants",
        data={"who": "Tifa", "set": "FIN", "q": "t", "extra": "{}"},
        follow_redirects=False,
    )
    assert int(r.headers["location"].rsplit("/", 1)[1]) != want_id


def test_form_error_keeps_values(env):
    _, _, client = env
    r = client.post(
        "/wants",
        data={
            "who": "Cloud",
            "set": "FIN",
            "q": "c",
            "id": "(",
            "max_any": "45",
            "refs": "cloud.jpg",
            "extra": "{}",
        },
    )
    assert "not a valid regular expression" in r.text
    assert 'value="45"' in r.text and 'value="cloud.jpg"' in r.text


def test_run_page_keeps_its_deals_after_a_later_price_drop(env, monkeypatch):
    s, db, client = env
    client.post(
        "/api/import",
        json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 900}}]},
    )
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    run1 = sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    sweep.run(db, s, browser=lambda: FakeBrowser([{**ROW, "price": "$500.00"}]))
    assert "Vegito" in client.get(f"/run/{run1}").text


def test_cross_site_write_is_refused(env):
    _, _, client = env
    r = client.post("/ops/run", headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def _sold(n, usd):
    return {
        str(900 + i): {
            "set": "DBS",
            "who": "Vegito",
            "kind": "psa",
            "star": "-",
            "usd": usd,
            "date": dt.date.today().isoformat(),
            "title": "Vegito PSA 10",
        }
        for i in range(n)
    }


def test_under_ceiling_but_above_sold_prices_is_not_a_deal(env, monkeypatch):
    s, db, client = env
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    client.post(
        "/api/import",
        json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 1150}}]},
    )
    client.post("/api/sold", json={"sold": _sold(3, 600)})
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))  # $705.64 landed, 118% of the median
    assert db.one("SELECT deal FROM listings WHERE item_id = '111'")["deal"] == 0
    cheap = {**BIN, "id": "333", "price": "$400.00"}
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW, cheap]))
    assert db.one("SELECT deal FROM listings WHERE item_id = '333'")["deal"] == 1
    assert db.one("SELECT deal FROM listings WHERE item_id = '111'")["deal"] == 0


def test_unticked_art_makes_a_photo_unwanted(tmp_path, monkeypatch):
    refs, shots = tmp_path / "refs", tmp_path / "photos"
    refs.mkdir()
    shots.mkdir()
    for n in ("a.jpg", "b.jpg"):
        (refs / n).write_bytes(b"x")
    monkeypatch.setattr(photos, "download", lambda src, dest: dest)
    w = {"set": "DBS", "refs": ["a.jpg", "b.jpg"], "skip": ["b.jpg"]}
    monkeypatch.setattr(photos, "score", lambda p, r: 60 if r.name == "b.jpg" else 5)
    assert photos.check(w, "1", "http://x", refs, shots)["verdict"] == "unwanted"
    monkeypatch.setattr(photos, "score", lambda p, r: 60)  # a kept art matches as well
    assert photos.check(w, "1", "http://x", refs, shots)["verdict"] == "match"
    rarity = {"UE22BT_CSM-1-048_p1": "SR★★", "UE22BT_CSM-1-048_p2": "SR★"}
    assert photos.label("UE22BT_CSM-1-048_p2.png", rarity) == "048 SR★"
    assert photos.label("UE22BT_CSM-1-048_p1.jpg", rarity) == "048 SR★★"
    assert photos.label("UE09BT_FMA-1-012_p2.jpg") == "012"  # no rarity known: no guessed stars
    assert photos.label("UEPR_FMA-1-010_p1.jpg") == "promo 010"
    assert photos.label("UE22BT_CSM-1-AP03.png") == "AP03"


def test_wants_cards_view_art_picker_and_defaults(env):
    _, db, client = env
    for n in ("a.jpg", "b.jpg"):
        client.put(f"/api/refs/{n}", content=b"x" * 2000)
    client.post(
        "/api/import",
        json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v", "refs": ["a.jpg", "b.jpg"]}]},
    )
    page = client.get("/wants")
    assert "/refs/a.jpg" in page.text and "2 of 2 arts" in page.text
    assert "<table" in client.get("/wants?view=table").text
    form = client.get("/wants/1").text
    assert 'name="keep" value="b.jpg"' in form
    r = client.post(
        "/wants",
        data={
            "_id": "1",
            "who": "Vegito",
            "set": "DBS",
            "q": "v",
            "refs": "a.jpg, b.jpg",
            "keep": ["a.jpg"],
            "art_all": ["a.jpg", "b.jpg"],
            "extra": "{}",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.want(1)["skip"] == ["b.jpg"]
    assert "1 of 2 arts" in client.get("/wants").text
    r = client.post(
        "/wants/defaults",
        data={"psa_1star_max": "70", "raw_1star_max": "$12"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.defaults()["raw_1star_max"] == 12
    assert client.post("/wants/defaults", data={"psa_1star_max": "x"}).status_code == 400
    nan = {"psa_1star_max": "nan", "raw_1star_max": "12"}
    assert client.post("/wants/defaults", data=nan).status_code == 400
    # an unticked art whose file has gone missing keeps its exclusion through an unrelated save
    db.run("UPDATE wants SET spec = json_set(spec, '$.skip', json('[\"gone.jpg\"]'))")
    client.post(
        "/wants",
        data={
            "_id": "1",
            "who": "Vegito",
            "set": "DBS",
            "q": "v",
            "refs": "a.jpg, b.jpg",
            "keep": ["a.jpg", "b.jpg"],
            "art_all": ["a.jpg", "b.jpg"],
            "extra": "{}",
            "enabled": "on",
        },
    )
    assert db.want(1)["skip"] == ["gone.jpg"]


def test_kept_art_match_in_gallery_beats_an_unwanted_thumbnail(env, monkeypatch):
    s, db, client = env
    client.post(
        "/api/import",
        json={
            "wants": [
                {"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 900}, "refs": ["a.jpg"]}
            ]
        },
    )
    calls = []

    def fake_check(_w, key, *_rest):
        calls.append(key)
        if key == "111":
            return {"verdict": "unwanted", "score": 100, "ref": "b.jpg", "photo": "p"}
        return {"verdict": "match", "score": 60, "ref": "a.jpg", "photo": "q"}

    class Gallery(FakeBrowser):
        def item_photos(self, _item_id):
            return ["http://x/1.jpg", "http://x/2.jpg"]

    monkeypatch.setattr(sweep.photos, "check", fake_check)
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    sweep.run(db, s, browser=lambda: Gallery([ROW]))
    row = db.one("SELECT deal, art FROM listings WHERE item_id = '111'")
    assert '"match"' in row["art"] and row["deal"] == 1
    assert calls == ["111", "111_0"]


def test_switching_raw_off_retires_its_listings(env, monkeypatch):
    s, db, client = env
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    client.post(
        "/api/import",
        json={
            "wants": [
                {
                    "set": "DBS",
                    "who": "Vegito",
                    "q": "v",
                    "rawq": "v raw",
                    "raw_max": 900,
                    "nostar": True,
                }
            ]
        },
    )
    raw = {
        **ROW,
        "title": "Vegito BT7-118 SPR Dragon Ball Super NM",
        "text": "Vegito BT7-118 $700.00",
    }
    sweep.run(db, s, browser=lambda: FakeBrowser([raw]))
    assert db.one("SELECT count(*) AS n FROM listings WHERE kind = 'raw' AND active = 1")["n"] == 1
    db.run("UPDATE wants SET spec = json_set(spec, '$.raw', json('false'))")
    sweep.run(db, s, browser=lambda: FakeBrowser([raw]))
    assert db.one("SELECT count(*) AS n FROM listings WHERE kind = 'raw' AND active = 1")["n"] == 0


def test_tiny_reference_cannot_prove_a_mismatch(tmp_path, monkeypatch):
    refs, shots = tmp_path / "refs", tmp_path / "photos"
    refs.mkdir()
    shots.mkdir()
    (refs / "a.png").write_bytes(b"x")
    monkeypatch.setattr(photos, "download", lambda src, dest: dest)
    monkeypatch.setattr(photos, "score", lambda p, r: 3)
    w = {"set": "DBS", "refs": ["a.png"]}
    monkeypatch.setattr(photos, "small", lambda r: True)
    assert photos.check(w, "1", "http://x", refs, shots)["verdict"] == "unclear"
    monkeypatch.setattr(photos, "small", lambda r: False)
    assert photos.check(w, "1", "http://x", refs, shots)["verdict"] == "differs"


def test_deal_page_shows_price_against_market(env, monkeypatch):
    s, db, client = env
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    client.post(
        "/api/import",
        json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 1150}}]},
    )
    client.post("/api/sold", json={"sold": _sold(3, 1000)})
    sweep.run(db, s, browser=lambda: FakeBrowser([BIN]))
    page = client.get("/deal/111/psa").text
    assert "71% of market" in page and "sold median $1,000" in page
    assert "aria-current=page" in page
    assert "71% of market" in client.get("/").text


def test_one_tiny_wanted_reference_keeps_a_low_score_unclear(tmp_path, monkeypatch):
    refs, shots = tmp_path / "refs", tmp_path / "photos"
    refs.mkdir()
    shots.mkdir()
    for n in ("tiny.png", "big.png"):
        (refs / n).write_bytes(b"x")
    monkeypatch.setattr(photos, "download", lambda src, dest: dest)
    monkeypatch.setattr(photos, "score", lambda p, r: 3)
    monkeypatch.setattr(photos, "small", lambda r: r.name == "tiny.png")
    w = {"set": "DBS", "refs": ["tiny.png", "big.png"]}
    assert photos.check(w, "1", "http://x", refs, shots)["verdict"] == "unclear"
    w["skip"] = ["tiny.png"]  # unticked, so only the large art counts
    assert photos.check(w, "1", "http://x", refs, shots)["verdict"] == "differs"


def test_runs_page_tolerates_odd_gem_rates(env):
    _, db, client = env
    db.put("gem_rates", {"FMA": "0.77", "EVA": None})
    db.put("defaults", {"psa_1star_max": "80", "raw_1star_max": None})
    page = client.get("/ops")
    assert page.status_code == 200 and "FMA 77%" in page.text and "EVA not set" in page.text
    assert "PSA 10 $80.00" in page.text


def test_imported_rarity_labels_the_art_picker(env):
    _, _, client = env
    client.put("/api/refs/UE22BT_CSM-1-048_p2.png", content=b"x" * 2000)
    client.post(
        "/api/import",
        json={
            "wants": [{"set": "CSM", "who": "Chainsaw Man", "refs": ["UE22BT_CSM-1-048_p2.png"]}],
            "ref_labels": {"UE22BT_CSM-1-048_p2": "SR★"},
        },
    )
    assert "048 SR★<" in client.get("/wants/1").text


def test_listed_prints_pick_exactly_those_refs(tmp_path):
    for n in ("UE22BT_CSM-1-053_p1.png", "UE22BT_CSM-1-056_p1.jpg", "UE22BT_CSM-1-055_p1.png"):
        (tmp_path / n).write_bytes(b"x")
    w = {"set": "CSM", "who": "Aki Hayakawa", "cards": "UE22BT/CSM-1-056_p1 SR★★"}
    assert [p.name for p in photos.refs_for(w, tmp_path)] == ["UE22BT_CSM-1-056_p1.jpg"]
    assert photos.listed_rarity(w) == {"UE22BT_CSM-1-056_p1": "SR★★"}


def test_bare_numbers_alongside_prints_keep_their_art(tmp_path):
    for n in ("UE15BT_EVA-1-082_p1.jpg", "UE15BT_EVA-1-081_p1.jpg"):
        (tmp_path / n).write_bytes(b"x")
    w = {"set": "EVA", "who": "Production Model-02", "cards": "UE15BT/EVA-1-082_p1 SR★, 081"}
    assert sorted(p.name for p in photos.refs_for(w, tmp_path)) == [
        "UE15BT_EVA-1-081_p1.jpg",
        "UE15BT_EVA-1-082_p1.jpg",
    ]


def test_changed_art_gets_a_cached_verdict_judged_again(env, monkeypatch):
    s, db, client = env
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    client.post(
        "/api/import",
        json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 900}}]},
    )
    calls = []

    def fake_check(w, key, *_rest):
        calls.append(key)
        refs = photos.ref_key(w, photos.refs_for(w, s.refs_dir))
        return {"verdict": "differs", "score": 3, "ref": "a", "photo": "p", "refs": refs}

    monkeypatch.setattr(sweep.photos, "check", fake_check)
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    first = len(calls)
    db.run("UPDATE wants SET spec = json_set(spec, '$.skip', json(?))", '["x.jpg"]')
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert len(calls) > first  # unticked art changed what the verdict means


SPLIT = [
    {
        "set": "FMA",
        "who": "Edward Elric · 012 SR★★",
        "match": "Edward Elric",
        "family": 900,
        "cards": "UE09BT/FMA-1-012_p1 SR★★",
        "max": {"2": 1000},
    },
    {
        "set": "FMA",
        "who": "Edward Elric · 040 SR★",
        "match": "Edward Elric",
        "family": 900,
        "cards": "UE09BT/FMA-1-040_p1 SR★",
        "max": {"1": 300},
    },
]


class CountingBrowser(FakeBrowser):
    calls = 0

    def search(self, url):
        CountingBrowser.calls += 1
        return super().search(url)


def ua_row(item_id, title, price="$200.00"):
    return {"id": item_id, "title": title, "price": price, "text": f"{title} {price}", "img": ""}


def test_split_prints_share_a_search_and_each_listing_has_one_print(env, monkeypatch):
    s, db, client = env
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    client.post("/api/import", json={"wants": [{**w, "raw": False} for w in SPLIT]})
    ids = {w["who"]: w["_id"] for w in db.wants()}
    rows = [
        ua_row("1", "Union Arena Edward Elric FMA-1-012 2 Star PSA 10"),  # named and starred
        ua_row("2", "Union Arena Edward Elric #040 1 Star PSA 10"),
        ua_row("3", "Union Arena Edward Elric PSA 10 gem mint"),  # the title can't place it
    ]
    CountingBrowser.calls = 0
    sweep.run(db, s, browser=lambda: CountingBrowser(rows))
    assert CountingBrowser.calls == 1  # one query for both prints
    got = {r["item_id"]: r["want_id"] for r in db.all("SELECT item_id, want_id FROM listings")}
    assert got["1"] == ids["Edward Elric · 012 SR★★"]
    assert got["2"] == ids["Edward Elric · 040 SR★"]
    # unplaced: no star in the title, so no ceiling on either print and no deal until the photo says
    assert db.one("SELECT deal FROM listings WHERE item_id = '3'")["deal"] == 0
    page = client.get(f"/?want={ids['Edward Elric · 012 SR★★']}&show=all")
    assert page.status_code == 200 and "Edit want" in page.text
    assert client.get("/?want=99999").status_code == 404
    wants_page = client.get("/wants").text
    assert f"/?want={ids['Edward Elric · 040 SR★']}&amp;show=deals" in wants_page


def test_sold_comps_for_a_split_card_go_to_its_family(env):
    _, db, client = env
    client.post("/api/import", json={"wants": SPLIT})
    sold = {
        "9": {
            "set": "FMA",
            "who": "Edward Elric",
            "kind": "psa",
            "star": "2",
            "usd": 900,
            "date": "2026-11-01",
            "title": "x",
        }
    }
    assert client.post("/api/sold", json={"sold": sold}).json()["added"] == 1
    assert db.one("SELECT want_id FROM sold")["want_id"] == 900


def test_deleting_a_family_want_keeps_comps_its_siblings_use(env):
    _, db, client = env
    client.post("/api/import", json={"wants": [{"set": "FMA", "who": "Edward Elric", "q": "e"}]})
    fam = db.wants()[0]["_id"]
    client.post("/api/import", json={"wants": [{**SPLIT[0], "family": fam}]})
    db.run(
        "INSERT INTO sold (item_id, want_id, kind, star, usd, date, title) "
        "VALUES ('s1', ?, 'psa', '2', 900, '2026-11-01', 'x')",
        fam,
    )
    client.post(f"/wants/{fam}/delete")
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 1
    client.post(f"/wants/{db.wants()[0]['_id']}/delete")  # the last print of the card
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 0


def test_a_cardhound_database_is_carried_over(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    (tmp_path / "cardhound.db").write_bytes(b"x" * 10)
    s = config.load()
    assert s.db_path.read_bytes() == b"x" * 10
    assert not (tmp_path / "cardhound.db").exists()


def test_deals_filter_by_graded_raw_format_and_sort(env):
    _, db, client = env
    client.post(
        "/api/import",
        json={
            "wants": [{"set": "DBS", "who": "Vegito", "q": "vegito psa 10", "max": {"any": 1150}}]
        },
    )
    wid = db.wants()[0]["_id"]

    def add(item_id, kind, usd, auction=0, ends=None, obo=0, seen="2026-11-05T12:00:00"):
        with db.tx() as c:
            c.execute(
                "INSERT INTO listings (item_id, kind, want_id, title, usd, star, auction, ends, obo, "
                "deal, first_seen, last_seen) VALUES (?, ?, ?, 'Vegito', ?, '-', ?, ?, ?, 1, ?, ?)",
                (item_id, kind, wid, usd, auction, ends, obo, seen, "2026-11-05T12:00:00"),
            )

    add("g1", "psa", 900, auction=1, ends="2026-11-07T13:00")
    add("g2", "psa", 800, obo=1, seen="2026-11-05T13:00:00")
    add("r1", "raw", 50, auction=1, ends="2026-11-05T12:35")

    def ids(qs):
        page = client.get("/" + qs).text
        return [x for x in ("g1", "g2", "r1") if f"/deal/{x}/" in page], page

    got, page = ids("?show=all")
    assert sorted(got) == ["g1", "g2", "r1"]
    assert 'Graded <span class="n">2</span>' in page and 'Raw <span class="n">1</span>' in page
    assert ids("?show=all&kind=psa")[0] == ["g1", "g2"]
    assert ids("?show=all&kind=raw")[0] == ["r1"]
    assert ids("?show=all&buy=auction")[0] == ["g1", "r1"]
    assert ids("?show=all&buy=offers")[0] == ["g2"]
    # counts follow the other filter: two auctions, one of them raw
    assert 'Raw <span class="n">1</span>' in ids("?show=all&buy=auction")[1]

    def order(qs):
        page = client.get("/" + qs).text
        return sorted(("g1", "g2", "r1"), key=lambda x: page.index(f"/deal/{x}/"))

    assert order("?show=all&sort=price") == ["r1", "g2", "g1"]
    assert order("?show=all&sort=ending")[:2] == ["r1", "g1"]  # auctions soonest first
    assert order("?show=all&sort=new")[0] == "g2"
    assert client.get("/?show=all&sort=bogus&buy=bogus&kind=bogus").status_code == 200


def test_sweep_stores_when_each_auction_ends(env, monkeypatch):
    s, db, client = env
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    client.post(
        "/api/import",
        json={
            "wants": [{"set": "DBS", "who": "Vegito", "q": "vegito psa 10", "max": {"any": 1150}}]
        },
    )
    short = {
        **ROW,
        "id": "333",
        "text": "PSA 10 Vegito BT7-118 SPR $700.00 +$5.64 delivery 2 bids 35m left",
    }
    before = dt.datetime.now()
    long = {**ROW, "text": ROW["text"].replace("35m", "1d 2h")}
    sweep.run(db, s, browser=lambda: FakeBrowser([long, short]))
    got = {r["item_id"]: r for r in db.all("SELECT item_id, time_left, ends FROM listings")}
    assert got["333"]["time_left"] == "35m left"
    end = dt.datetime.fromisoformat(got["333"]["ends"])
    assert dt.timedelta(minutes=33) < end - before < dt.timedelta(minutes=37)
    assert got["111"]["time_left"] == "1d 2h left"
    page = client.get("/?show=all&sort=ending").text
    assert page.index("/deal/333/") < page.index("/deal/111/")


def test_deals_list_hides_a_photo_of_another_card(env):
    _, db, client = env
    client.post(
        "/api/import",
        json={"wants": [{"set": "EVA", "who": "Rei", "q": "rei", "max": {"any": 300}}]},
    )
    wid = db.wants()[0]["_id"]
    for item_id, verdict in (("ok1", "match"), ("bad1", "differs"), ("new1", None)):
        art = json.dumps({"verdict": verdict}) if verdict else None
        with db.tx() as c:
            c.execute(
                "INSERT INTO listings (item_id, kind, want_id, title, usd, star, deal, art, "
                "first_seen, last_seen) VALUES (?, 'psa', ?, 'Rei', 100, '-', 1, ?, "
                "'2026-11-08T12:00:00', '2026-11-08T12:00:00')",
                (item_id, wid, art),
            )
    deals = client.get("/").text
    assert "/deal/ok1/" in deals and "/deal/new1/" in deals
    assert "/deal/bad1/" not in deals
    assert "/deal/bad1/" in client.get("/?show=all").text
    assert "sleeper_live_deals 2" in client.get("/metrics").text
    search = client.get("/api/search").json()
    assert sorted(d["href"].split("/")[2] for d in search["deals"]) == ["new1", "ok1"]
    assert search["wants"][0]["side"] == "2 deals, 3 live"
