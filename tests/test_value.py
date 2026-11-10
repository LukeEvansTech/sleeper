import datetime as dt

from sleeper import value

TODAY = dt.date(2026, 11, 4)
W = {"_id": 1, "set": "FMA", "who": "Edward Elric"}


def sold(kind, star, *prices):
    return [
        {"want_id": 1, "kind": kind, "star": star, "usd": p, "date": "2026-10-20"} for p in prices
    ]


def test_comps_window_and_median():
    rows = sold("psa", "2", 100, 200, 300) + [
        {"want_id": 1, "kind": "psa", "star": "2", "usd": 999, "date": "2026-01-01"}
    ]
    c = value.comps(rows, TODAY)[(1, "psa", "2")]
    assert (c.median, c.count, c.low, c.high) == (200, 3, 100, 300)


def listing(kind="psa", usd=150.0, ship=0.0, cap=1000.0, auction=0):
    return {"kind": kind, "star": "2", "usd": usd, "ship": ship, "cap": cap, "auction": auction}


def test_slab_below_median_is_great_buy():
    cmp = value.comps(sold("psa", "2", 200, 210, 220), TODAY)
    v = value.assess(listing(usd=140), W, cmp, [], {"FMA": 0.77}, 39.99, 6)
    assert v.rating == "great"
    assert "Buy the slab" in v.advice


def test_slab_where_grading_is_much_cheaper_says_so():
    cmp = value.comps(sold("psa", "2", 1000, 1100, 1200) + sold("raw", "2", 150, 160), TODAY)
    v = value.assess(listing(usd=1050), W, cmp, [], {"FMA": 0.77}, 39.99, 6)
    assert v.grade_cost is not None and v.grade_cost < 300
    assert "raw and grading is cheaper" in v.advice


def test_raw_to_grade_uses_card_pop_when_enough_graded():
    pop = [{"descr": "Edward Elric 2 Star 012", "gems": 5, "total": 10, "set_name": "x"}]
    cmp = value.comps(sold("psa", "2", 1000), TODAY)
    v = value.assess(listing(kind="raw", usd=200), W, cmp, pop, {"FMA": 0.77}, 39.99, 6)
    assert round(v.grade_cost) == round((200 + 39.99 + 6) / 0.5)
    assert "Buy to grade" in v.advice
    assert any("this card's PSA pop (5/10)" in r for r in v.reasons)


def test_no_history_under_ceiling_is_good_not_unknown():
    v = value.assess(listing(usd=50, cap=80), W, {}, [], {}, 39.99, 6)
    assert v.rating == "good"


def test_auction_gets_a_max_bid_under_ceiling_and_median():
    cmp = value.comps(sold("psa", "2", 500), TODAY)
    v = value.assess(listing(usd=100, ship=10, cap=400, auction=1), W, cmp, [], {}, 39.99, 6)
    assert v.max_bid == 390


def test_gem_rate_stays_in_its_star_bucket():
    pop = [
        {"descr": "Edward Elric 2 Star 012", "gems": 1, "total": 10, "set_name": "x"},
        {"descr": "Edward Elric 012", "gems": 99, "total": 100, "set_name": "x"},
    ]
    rate, _ = value.gem_rate(W, "2", pop, {})
    assert rate == 0.1


def test_max_bid_stays_a_deal_at_the_new_cutoff():
    cmp = value.comps(sold("psa", "2", 100), TODAY)
    v = value.assess(listing(usd=50, ship=0, cap=100, auction=1), W, cmp, [], {}, 39.99, 6)
    assert v.max_bid == 92
    at_bid = value.assess(listing(usd=v.max_bid, cap=100), W, cmp, [], {}, 39.99, 6)
    assert at_bid.is_deal
    raw = value.comps(sold("raw", "2", 50), TODAY)
    v = value.assess(listing(kind="raw", usd=10, cap=60, auction=1), W, raw, [], {}, 39.99, 6)
    assert v.max_bid == 46


def test_max_bid_never_rounds_past_the_cutoff():
    cmp = value.comps(sold("psa", "2", 139.25), TODAY)
    v = value.assess(listing(usd=50, cap=1000, auction=1), W, cmp, [], {}, 39.99, 6)
    assert v.max_bid == 128
    assert value.assess(listing(usd=v.max_bid, cap=1000), W, cmp, [], {}, 39.99, 6).is_deal


def test_infinite_ceiling_gives_no_bid_rather_than_crashing():
    v = value.assess(listing(usd=50, cap=float("inf"), auction=1), W, {}, [], {}, 39.99, 6)
    assert v.max_bid is None


def test_snipe_bid_skips_round_amounts_and_stays_under_the_limit():
    assert value.snipe_bid(103.00) == 102.98  # .00 and .99 are where other bidders sit
    assert value.snipe_bid(102.87) == 102.87
    assert value.snipe_bid(50.05) == 50.04
    assert value.snipe_bid(10.01) == 9.98
    assert value.snipe_bid(77.23) == 77.23
    assert value.snipe_bid(0.0) is None and value.snipe_bid(float("inf")) is None
    for limit in (1.07, 33.3, 250.1, 999.999):  # never above the limit, never a skipped ending
        got = value.snipe_bid(limit)
        c = round(got * 100) % 100
        assert got <= limit and c % 5 and c not in (1, 99)


def test_snipe_bid_is_none_below_the_next_minimum_bid():
    assert value.snipe_bid(102.98, current=100.0) == 102.98  # 100 + 2.50 is 102.50
    assert value.snipe_bid(102.40, current=100.0) is None
    assert value.snipe_bid(102.50, current=100.0) is None  # steps down to 102.48: under the minimum
    assert value.snipe_bid(20.04, current=20.04, bids=0) == 20.04
    assert value.snipe_bid(20.04, current=20.04, bids=1) is None  # one more increment is needed
    assert [value.increment(x) for x in (0.5, 3, 30, 120, 300, 600, 1500, 3000, 6000)] == [
        0.05, 0.25, 1, 2.5, 5, 10, 25, 50, 100,
    ]  # fmt: skip


NOW = dt.datetime(2026, 11, 10, 12, 0)


def auction(usd, bids="3 bids", hours=30, **kw):
    ends = (NOW + dt.timedelta(hours=hours)).isoformat(timespec="minutes")
    return {**listing(usd=usd, auction=1, **kw), "bids": bids, "ends": ends}


def test_early_low_bid_is_not_a_deal_while_the_auction_runs():
    cmp = value.comps(sold("psa", "2", 700, 720, 740), TODAY)
    a = auction(33, ship=0, cap=200)  # $33 now, but these finish near $720
    v = value.assess(a, W, cmp, [], {}, 39.99, 6, now=NOW)
    assert not v.is_deal and v.expected == 720 and v.rating in ("fair", "high")
    assert "likely to finish near $720" in v.advice and "bid up to" in v.advice
    # the same listing as buy it now, or once the auction is over, is rated on its price
    assert value.assess({**a, "auction": 0}, W, cmp, [], {}, 39.99, 6, now=NOW).is_deal
    done = auction(33, hours=-1, cap=200)
    assert value.assess(done, W, cmp, [], {}, 39.99, 6, now=NOW).is_deal


def test_auction_without_comps_is_a_deal_only_in_its_final_hour():
    early = value.assess(auction(10, hours=30, cap=200), W, {}, [], {}, 39.99, 6, now=NOW)
    assert early.rating == "unknown" and not early.is_deal
    last = value.assess(auction(10, hours=0.5, cap=200), W, {}, [], {}, 39.99, 6, now=NOW)
    assert last.is_deal


def test_max_bid_is_under_ceiling_and_95_percent_of_the_median_less_delivery():
    cmp = value.comps(sold("psa", "2", 500), TODAY)
    # ceiling 470, 0.95 x 500 = 475, 0.92 x 500 = 460 -> the 460 limit, less $10 delivery
    v = value.assess(auction(100, ship=10, cap=470), W, cmp, [], {}, 39.99, 6, now=NOW)
    assert v.max_bid == 450 and v.snipe == 449.98
    assert "92% of the PSA 10 sold median $500" in v.limit_why and "$10.00 delivery" in v.limit_why
    # a ceiling lower than that wins
    v = value.assess(auction(100, ship=0, cap=300), W, cmp, [], {}, 39.99, 6, now=NOW)
    assert v.snipe == 299.98 and "your ceiling of $300" in v.limit_why


def test_limit_below_the_next_bid_gives_no_max_bid():
    cmp = value.comps(sold("psa", "2", 100), TODAY)
    v = value.assess(auction(95, cap=100), W, cmp, [], {}, 39.99, 6, now=NOW)  # limit 92 < bid 95
    assert v.snipe is None and v.max_bid is None
    assert "below the next minimum bid" in v.advice
