"""Outcome recording: what eBay says about listings that dropped out of the search."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sleeper import config, outcomes, sweep
from sleeper.db import DB
from sleeper.web import create_app
from test_web import ROW, FakeBrowser

NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)
PAST = "2026-11-09T18:00:00.000Z"


def item(price="72.00", status="OUT_OF_STOCK", sold=1, bids=25, end=PAST, buying=("AUCTION",)):
    return {
        "price": {"value": price, "currency": "USD"},
        "bidCount": bids,
        "itemEndDate": end,
        "buyingOptions": list(buying),
        "estimatedAvailabilities": [
            {"estimatedAvailabilityStatus": status, "estimatedSoldQuantity": sold}
        ],
    }


class FakeAPI(FakeBrowser):
    def __init__(self, answers):
        super().__init__([])
        self.answers, self.asked = answers, []

    def item(self, item_id):
        self.asked.append(item_id)
        got = self.answers.get(item_id)
        if isinstance(got, Exception):
            raise got
        return got


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "s.db")
    d.save_want({"set": "DBS", "who": "Vegito", "q": "v"})  # id 1
    d.save_want({"set": "DBS", "who": "Vegito 2", "q": "v", "family": 1})  # id 2: a split print
    return d


def listing(db, item_id, want_id=1, active=0, auction=1, obo=0, ends=None):
    db.run(
        """INSERT INTO listings (item_id, kind, want_id, title, usd, ship, star, auction, obo, cap,
               deal, first_seen, last_seen, low_usd, active, ends, cur)
           VALUES (?, 'psa', ?, 'PSA 10 Vegito', 33, 5, '2', ?, ?, 40, 1, '2026-11-01', '2026-11-08',
               30, ?, ?, 'USD')""",
        item_id,
        want_id,
        auction,
        obo,
        active,
        ends,
    )


def outcome(db, item_id):
    r = db.one("SELECT * FROM outcomes WHERE item_id = ?", item_id)
    return dict(r) if r else None


def test_a_sold_auction_is_recorded_and_joins_the_comps_under_its_family(db):
    listing(db, "1", want_id=2)
    api = FakeAPI({"1": item()})
    got = outcomes.check(db, api, NOW)
    assert got["sold"] == 1
    o = outcome(db, "1")
    assert (o["status"], o["final_usd"], o["bids"], o["sold_qty"]) == ("sold", 72.0, 25, 1)
    s = db.one("SELECT * FROM sold WHERE item_id = '1'")
    # price only (as sold search rows carry it), under the family want, dated by the end
    assert (s["want_id"], s["kind"], s["star"], s["usd"], s["date"]) == (
        1,
        "psa",
        "2",
        72.0,
        "2026-11-09",
    )
    outcomes.check(db, api, NOW + dt.timedelta(days=2))
    assert api.asked == ["1"]  # a final outcome is never asked about again
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 1


def test_an_existing_sold_row_is_not_overwritten(db):
    listing(db, "1")
    db.run("INSERT INTO sold VALUES ('1', 1, 'psa', '2', 99.0, '2026-11-01', 'from the Mac')")
    outcomes.check(db, FakeAPI({"1": item()}), NOW)
    assert db.one("SELECT usd FROM sold WHERE item_id = '1'")["usd"] == 99.0


def test_an_ended_auction_with_no_sale_is_unsold_and_adds_no_comp(db):
    listing(db, "2")
    outcomes.check(db, FakeAPI({"2": item("33.00", sold=0, bids=0)}), NOW)
    assert outcome(db, "2")["status"] == "unsold"
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 0


def test_a_listing_still_on_sale_is_asked_again_after_20_hours_and_dropped_after_30_days(db):
    listing(db, "3", auction=0)
    live = item("40.00", status="IN_STOCK", sold=2, end=None, buying=("FIXED_PRICE",))
    api = FakeAPI({"3": live})
    outcomes.check(db, api, NOW)
    assert outcome(db, "3")["status"] == "on_sale"  # sold some, but still listed: not final
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 0
    outcomes.check(db, api, NOW + dt.timedelta(hours=19))
    assert api.asked == ["3"]
    outcomes.check(db, api, NOW + dt.timedelta(hours=21))
    assert api.asked == ["3", "3"]
    outcomes.check(db, api, NOW + dt.timedelta(days=31))
    assert api.asked == ["3", "3"]


def test_a_failed_call_is_noted_and_never_raises(db):
    listing(db, "4")
    listing(db, "5")
    api = FakeAPI({"4": None, "5": RuntimeError("boom")})
    got = outcomes.check(db, api, NOW)
    assert got["error"] == 2 and outcome(db, "4")["status"] == "error"
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 0
    api.answers["4"] = item()
    outcomes.check(db, api, NOW + dt.timedelta(hours=21))  # tried again later, and it worked
    assert outcome(db, "4")["status"] == "sold"


def test_consecutive_failures_stop_the_pass(db):
    for n in range(10):
        listing(db, f"9{n}")
    api = FakeAPI({})
    outcomes.check(db, api, NOW)
    assert len(api.asked) == outcomes.STOP_AFTER_FAILURES


def test_calls_per_pass_are_capped_and_the_rest_wait(db):
    for n in range(5):
        listing(db, f"7{n}")
    api = FakeAPI({f"7{n}": item() for n in range(5)})
    outcomes.check(db, api, NOW, limit=3)
    assert len(api.asked) == 3
    outcomes.check(db, api, NOW, limit=3)
    assert len(api.asked) == 5  # the two left, not the three done


def test_live_listings_and_auctions_still_running_are_not_asked(db):
    listing(db, "6", active=1, auction=0)
    future = (NOW.astimezone().replace(tzinfo=None) + dt.timedelta(hours=3)).isoformat()
    listing(db, "7", active=1, ends=future)
    past = (NOW.astimezone().replace(tzinfo=None) - dt.timedelta(hours=3)).isoformat()
    listing(db, "8", active=1, ends=past)  # ended, not yet dropped from the search
    api = FakeAPI({"8": item()})
    outcomes.check(db, api, NOW)
    assert api.asked == ["8"]


def test_a_sweep_survives_an_outcome_lookup_that_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    s = config.load()
    d = DB(s.db_path)
    d.save_want({"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 1150}})
    listing(d, "55")

    class Boom(FakeBrowser):
        def item(self, _id):
            raise RuntimeError("down")

    run = sweep.run(d, s, browser=lambda: Boom([ROW]))
    assert d.one("SELECT status FROM runs WHERE id = ?", run)["status"] == "success"


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    s = config.load()
    d = DB(s.db_path)
    d.save_want({"set": "DBS", "who": "Vegito", "q": "v"})
    return d, TestClient(create_app(s, d))


def test_ended_page_lists_an_ended_deal_and_not_a_live_one(web):
    d, client = web
    listing(d, "100")  # a deal that went inactive and finished at $72
    listing(d, "200", active=1, auction=0)  # a live deal
    listing(d, "300")  # ended, but never a deal or alert
    d.run("UPDATE listings SET deal = 0 WHERE item_id = '300'")
    listing(d, "400")  # not a deal now, but it was alerted
    d.run("UPDATE listings SET deal = 0 WHERE item_id = '400'")
    d.run("INSERT INTO alerts (item_id, kind, usd) VALUES ('400', 'psa', 38)")
    outcomes.check(d, FakeAPI({n: item() for n in ("100", "300", "400")}), NOW)
    html = client.get("/ended").text
    assert "/deal/100/psa" in html and "/deal/400/psa" in html
    assert "/deal/200/" not in html and "/deal/300/" not in html
    assert "$72.00" in html and "25 bids" in html and "Sold" in html and "over by" in html
    assert 'href="/ended"' in client.get("/").text  # in the main nav
    assert "/deal/100/" not in client.get("/").text  # not on the live deals list
    deal = client.get("/deal/100/psa").text
    assert "How it ended" in deal and "$72.00" in deal


def test_ended_page_orders_newest_first(web):
    d, client = web
    listing(d, "1")
    listing(d, "2")
    outcomes.check(
        d,
        FakeAPI({"1": item(end="2026-11-01T10:00:00Z"), "2": item(end="2026-11-08T10:00:00Z")}),
        NOW,
    )
    html = client.get("/ended").text
    assert html.index("/deal/2/") < html.index("/deal/1/")


def test_an_unmet_reserve_is_not_a_sale(db):
    listing(db, "1")
    got = item(sold=None, bids=3)
    got["estimatedAvailabilities"] = [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]
    outcomes.check(db, FakeAPI({"1": {**got, "reservePriceMet": False}}), NOW)
    assert outcome(db, "1")["status"] == "unsold"
    listing(db, "2")
    outcomes.check(db, FakeAPI({"2": got}), NOW)  # no sold count: bids decide
    assert outcome(db, "2")["status"] == "sold"


def test_a_rejected_listing_never_becomes_a_comp(db):
    listing(db, "1")
    listing(db, "2")
    db.run("INSERT INTO blocked (item_id, reason) VALUES ('1', 'slab is CGC 9')")
    db.run("""UPDATE listings SET art = '{"verdict": "differs"}' WHERE item_id = '2'""")
    outcomes.check(db, FakeAPI({"1": item(), "2": item()}), NOW)
    assert outcome(db, "1")["status"] == "sold"
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 0


def test_a_listing_found_ended_is_retired_and_not_rated_again(db):
    listing(db, "1", active=1, ends="2026-11-10T10:00")  # ended, still marked live
    outcomes.check(db, FakeAPI({"1": item()}), NOW)
    assert db.one("SELECT active FROM listings WHERE item_id = '1'")["active"] == 0


def test_best_offer_outcome_shows_the_asking_price_not_a_final(web):
    d, client = web
    listing(d, "1", auction=0, obo=1)
    outcomes.check(d, FakeAPI({"1": item(buying=("FIXED_PRICE",))}), NOW)
    html = client.get("/ended").text
    assert "Listed at" in html and "asking price" in html and "<dt>Final</dt>" not in html


def test_max_bid_keeps_its_cents(web):
    d, client = web
    listing(d, "1", active=1, ends="2099-01-01T00:00")
    d.run("UPDATE listings SET usd = 480, cap = 500, ship = 0, bids = '2 bids', cur = 'USD'")
    html = client.get("/?show=all").text
    assert "Max bid $499.98" in html and "max bid $499.98" in html


def test_sold_out_stock_without_an_end_date_is_not_final(db):
    listing(db, "1", auction=0)
    got = item(end=None, buying=("FIXED_PRICE",))  # OUT_OF_STOCK, sold 1, no end: may be refilled
    outcomes.check(db, FakeAPI({"1": got}), NOW)
    assert outcome(db, "1")["status"] == "on_sale"


def test_fixed_price_sales_are_not_comps_but_auction_sales_are(db):
    listing(db, "1", auction=0)
    outcomes.check(db, FakeAPI({"1": item(buying=("FIXED_PRICE",))}), NOW)  # ended by date
    assert outcome(db, "1")["status"] == "sold"
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 0


def test_a_finished_listing_stays_retired_when_a_search_returns_it(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    s = config.load()
    d = DB(s.db_path)
    d.save_want({"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 1150}})
    sweep.run(d, s, browser=lambda: FakeBrowser([ROW]))
    assert d.one("SELECT active FROM listings WHERE item_id = '111'")["active"] == 1
    d.run(
        "INSERT INTO outcomes (item_id, status, checked_at, first_checked_at) "
        "VALUES ('111', 'sold', '2026-11-10', '2026-11-10')"
    )
    sweep.run(d, s, browser=lambda: FakeBrowser([ROW]))
    assert d.one("SELECT active FROM listings WHERE item_id = '111'")["active"] == 0


def test_an_auction_records_its_winning_bid_not_the_buy_it_now_price(db):
    listing(db, "1")
    got = {**item("200.00"), "currentBidPrice": {"value": "72.00", "currency": "USD"}}
    outcomes.check(db, FakeAPI({"1": got}), NOW)
    assert outcome(db, "1")["final_usd"] == 72.0
    assert db.one("SELECT usd FROM sold WHERE item_id = '1'")["usd"] == 72.0


def test_a_sale_with_no_price_is_asked_about_again(db):
    listing(db, "1")
    got = item()
    got["price"] = {}
    api = FakeAPI({"1": got})
    outcomes.check(db, api, NOW)
    assert outcome(db, "1")["status"] == "on_sale"
    api.answers["1"] = item()
    outcomes.check(db, api, NOW + dt.timedelta(hours=21))
    assert outcome(db, "1")["status"] == "sold"


def test_a_finished_listing_keeps_its_deal_flag_when_a_search_returns_it(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    s = config.load()
    d = DB(s.db_path)
    d.save_want({"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 1150}})
    sweep.run(d, s, browser=lambda: FakeBrowser([ROW]))
    d.run("UPDATE listings SET deal = 0 WHERE item_id = '111'")  # rated down while it ran
    d.run(
        "INSERT INTO outcomes (item_id, status, checked_at, first_checked_at) "
        "VALUES ('111', 'sold', '2026-11-10', '2026-11-10')"
    )
    sweep.run(d, s, browser=lambda: FakeBrowser([ROW]))
    assert d.one("SELECT deal FROM listings WHERE item_id = '111'")["deal"] == 0


def test_an_ended_fixed_price_listing_with_no_sales_count_stays_unresolved(db):
    listing(db, "1", auction=0)
    got = item(buying=("FIXED_PRICE",))
    got["estimatedAvailabilities"] = [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]
    outcomes.check(db, FakeAPI({"1": got}), NOW)
    assert outcome(db, "1")["status"] == "on_sale"


def test_an_auction_closed_without_bids_adds_no_comp_and_missing_counts_wait(db):
    listing(db, "1")
    listing(db, "2")
    offer = item(bids=0)  # sold with no bids: a Best Offer, price unknown
    blank = item(sold=None)
    del blank["bidCount"]
    blank["estimatedAvailabilities"] = [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]
    outcomes.check(db, FakeAPI({"1": offer, "2": blank}), NOW)
    assert outcome(db, "1")["status"] == "sold" and outcome(db, "2")["status"] == "on_sale"
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 0
