"""Is a listing good value, and is it cheaper to buy a raw copy and grade it?

Everything here is pure: it takes the listing, the want, sold comps and PSA pop, and returns the
verdict the deal page and the alert show.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from dataclasses import dataclass, field

UA_POP_TAG = {"FMA": "fma", "EVA": "eva", "CSM": "csm", "AOT": "aot"}


@dataclass
class Comp:
    median: float
    count: int
    last: str
    low: float
    high: float


def comps(sold: list[dict], today: dt.date, days: int = 90) -> dict[tuple[int, str, str], Comp]:
    """(want_id, kind, star) -> sold-price summary over the last `days` days."""
    cut = (today - dt.timedelta(days=days)).isoformat()
    by: dict[tuple[int, str, str], list[tuple[float, str]]] = {}
    for x in sold:
        if x["date"] >= cut and x["usd"] > 0:
            by.setdefault((x["want_id"], x["kind"], x["star"]), []).append((x["usd"], x["date"]))
    return {
        k: Comp(
            median=statistics.median(p for p, _ in v),
            count=len(v),
            last=max(d for _, d in v),
            low=min(p for p, _ in v),
            high=max(p for p, _ in v),
        )
        for k, v in by.items()
    }


def star_of(desc: str) -> str:
    m = re.search(r"(\d) star", desc.lower())
    return m.group(1) if m else "-"


def pop_for(pop: list[dict], w: dict) -> list[dict]:
    """PSA pop rows for one want: its pop term and row regex, else every name word in its set."""
    if w.get("pop"):
        term, row = w["pop"].get("term"), w["pop"].get("row", "")
        hits = [p for p in pop if p.get("term") == term and re.search(row, p["descr"], re.I)]
        return sorted(hits, key=lambda p: -p["total"])
    tag = UA_POP_TAG.get(w.get("set", ""))
    if not tag:
        return []
    words = (w.get("match") or w["who"]).lower().split()
    hits = []
    for p in pop:
        d, s = p["descr"].lower(), p["set_name"].lower()
        if ("mari makinami" in d and "mari" not in words) or (tag not in s and "promo" not in s):
            continue
        if all(re.search(rf"(?<![a-z0-9]){re.escape(x)}(?![a-z0-9])", d) for x in words):
            hits.append(p)
    return sorted(hits, key=lambda p: -p["total"])


def gem_rate(w: dict, star: str, pop_rows: list[dict], set_rates: dict) -> tuple[float | None, str]:
    """Chance a raw copy grades PSA 10: the card's own pop when 10+ have been graded, else the set's."""
    rows = [p for p in pop_rows if star_of(p["descr"]) == star]
    gems, total = sum(p["gems"] for p in rows), sum(p["total"] for p in rows)
    if total >= 10:
        return gems / total, f"this card's PSA pop ({gems}/{total})"
    rate = set_rates.get(w.get("set", ""))
    if rate:
        return float(rate), f"the {w['set']} set average"
    return None, ""


@dataclass
class Verdict:
    rating: str  # great | good | fair | high | unknown
    headline: str
    advice: str
    reasons: list[str] = field(default_factory=list)
    total: float = 0.0
    grade_cost: float | None = None
    max_bid: float | None = None  # the limit in whole dollars; the Gixen amount is `snipe`
    snipe: float | None = None
    limit_why: str = ""
    expected: float | None = None  # a running auction's likely finish, landed
    held: bool = False  # a running auction is never a deal on its bid so far
    buy_to_grade: bool = False
    median: float | None = None  # same kind and star bucket, 90 days
    low: float | None = None
    high: float | None = None
    # a running auction that usually sells within the ceiling and can still be bid on
    snipeable: bool = False

    @property
    def market(self) -> float | None:
        """Landed price as a share of the sold median (1.0 = market price)."""
        return self.total / self.median if self.median else None

    @property
    def is_deal(self) -> bool:
        """Under the ceiling is not enough: the price must also beat recent sales, or grading must pay."""
        if self.held:
            return False
        if self.snipeable:
            return True
        return self.rating in ("great", "good", "unknown") or self.buy_to_grade


# eBay US bid increments: (current bid at or above, increment), in cents.
INCREMENTS = [
    (0, 5),
    (100, 25),
    (500, 50),
    (2500, 100),
    (10000, 250),
    (25000, 500),
    (50000, 1000),
    (100000, 2500),
    (250000, 5000),
    (500000, 10000),
]
FINAL_WINDOW_H = 6.0  # the default sweep interval: seen once in this window, it is rated on its bid


def increment(current: float) -> float:
    """The smallest rise eBay accepts over this current bid (US table, USD)."""
    cents = round(current * 100)
    step = [inc for floor, inc in INCREMENTS if cents >= floor][-1]
    return step / 100


def snipe_bid(limit: float, current: float | None = None, bids: int = 1) -> float | None:
    """The amount to enter in Gixen: just under the limit, avoiding the round amounts other bidders use.

    Steps down from the limit a cent at a time until the cents are not a multiple of 5 and not
    .01 or .99. None when the limit is below the next minimum bid (current bid plus one increment,
    or the opening price itself while there are no bids), or when nothing positive is left."""
    if not math.isfinite(limit) or limit <= 0:
        return None
    cents = math.floor(round(limit * 100, 6))
    while cents > 0 and (cents % 5 == 0 or cents % 100 in (1, 99)):
        cents -= 1
    if cents <= 0:
        return None
    if current is not None:
        need = round(current * 100) + (round(increment(current) * 100) if bids else 0)
        if cents < need:
            return None
    return cents / 100


def hours_left(listing: dict, now: dt.datetime | None = None) -> float | None:
    """Hours until an auction ends (negative once it has), None when its end is not known."""
    try:
        at = dt.datetime.fromisoformat(listing["ends"])
    except (KeyError, TypeError, ValueError):
        return None
    return (at - (now or dt.datetime.now())).total_seconds() / 3600


def _bid_count(listing: dict) -> int:
    m = re.search(r"\d+", listing.get("bids") or "")
    return int(m.group(0)) if m else 0


def _pct(a: float, b: float) -> str:
    return f"{a / b:.0%}"


def assess(
    listing: dict,
    w: dict,
    cmp: dict,
    pop_rows: list[dict],
    set_rates: dict,
    fee: float,
    grade_ship: float,
    now: dt.datetime | None = None,
) -> Verdict:
    kind, star, usd = listing["kind"], listing["star"], float(listing["usd"])
    ship = listing.get("ship")
    total = usd + (ship or 0)
    cap = listing.get("cap")
    left = hours_left(listing, now) if listing.get("auction") else None
    running = bool(listing.get("auction")) and (left is None or left > 0)
    wid = int(w.get("family") or w["_id"])  # split prints share their card's comps
    ten, raw = cmp.get((wid, "psa", star)), cmp.get((wid, "raw", star))
    rate, rate_src = gem_rate(w, star, pop_rows, set_rates)
    reasons: list[str] = []
    ship_txt = (
        "free delivery" if ship == 0 else f"${ship:,.2f} delivery" if ship else "delivery not shown"
    )
    reasons.append(f"${usd:,.2f} + {ship_txt} = ${total:,.2f} landed at the Oregon address")

    if cap is not None:
        if total <= cap:
            reasons.append(f"Under your ceiling of ${cap:,.0f} by ${cap - total:,.0f}")
        else:
            reasons.append(f"Over your ceiling of ${cap:,.0f} by ${total - cap:,.0f}")

    same = ten if kind == "psa" else raw
    label = "PSA 10" if kind == "psa" else "raw"
    ratio = None
    expected = total
    if running and same:
        # Bids rise, mostly at the end: judge the likely finish, not the bid so far.
        expected = max(total, same.median)
    if same:
        ratio = expected / same.median
        reasons.append(
            f"{_pct(total, same.median)} of the {label} sold median ${same.median:,.0f} "
            f"({same.count} sale{'s' * (same.count != 1)} in 90 days, ${same.low:,.0f}-${same.high:,.0f}, "
            f"last {same.last})"
        )
        if running and expected > total:
            reasons.append(
                f"Auction still running: expected to finish near the ${same.median:,.0f} median, "
                f"not the ${total:,.2f} bid so far"
            )
    else:
        reasons.append(f"No {label} sales of this card recorded in the last 90 days")

    grade_cost = None
    buy_to_grade = False
    if rate:
        base, what = (expected, "this copy") if kind == "raw" else (None, "")
        if kind != "raw" and raw:
            base, what = raw.median, f"a raw copy at the ${raw.median:,.0f} median"
        if base is not None:
            grade_cost = (base + fee + grade_ship) / rate
            reasons.append(
                f"Grading {what}: ${base:,.0f} + ${fee:.2f} PSA Value + ${grade_ship:.0f} shipping, "
                f"÷ {rate:.0%} gem rate ({rate_src}) = ${grade_cost:,.0f} per PSA 10"
            )

    if ratio is None:
        rating = "unknown" if cap is None or total > cap else "good"
    elif ratio <= 0.75:
        rating = "great"
    elif ratio <= 0.92:
        rating = "good"
    elif ratio <= 1.08:
        rating = "fair"
    else:
        rating = "high"

    held = False
    if running:
        if not same:  # no comps: the finish is unknowable, so only the last hour counts
            held = left is None or left > FINAL_WINDOW_H
            if held:
                rating = "unknown"
        elif cap is not None and expected > cap:
            held = True  # even the likely finish is over the ceiling

    if kind == "psa":
        if grade_cost and grade_cost < total * 0.8:
            advice = (
                f"Buying raw and grading is cheaper (~${grade_cost:,.0f} per 10 against ${total:,.0f}), "
                "but takes weeks and the gem rate is a gamble."
            )
        elif rating in ("great", "good") and ten:
            advice = "Buy the slab: the price is below what PSA 10s have been selling for."
        elif rating == "good":
            advice = "Under your ceiling, but there are no PSA 10 sales to compare it with."
        elif rating == "fair":
            advice = "Fair market price. Buy if you want it now; otherwise offer below the median."
        elif rating == "high":
            advice = "Pass or offer: priced above recent PSA 10 sales."
        else:
            advice = "No sales history yet: judge it on your ceiling alone."
    else:
        if grade_cost and ten:
            if grade_cost < ten.median * 0.85:
                buy_to_grade = True
                advice = f"Buy to grade: ~${grade_cost:,.0f} per PSA 10 against a ${ten.median:,.0f} slab median."
            else:
                advice = (
                    f"Grading this costs ~${grade_cost:,.0f} per 10, close to the ${ten.median:,.0f} "
                    "slab median: buy the slab instead unless you want the raw."
                )
        elif rating in ("great", "good") and ten:
            advice = (
                f"Good raw price. No gem rate for {w.get('set', 'this set')} yet, so the cost of "
                f"grading it cannot be set against the ${ten.median:,.0f} slab median."
            )
        elif rating in ("great", "good"):
            advice = "Good raw price. No PSA 10 sales yet to compare a graded copy against."
        else:
            advice = "Judge on the photos and your ceiling; there is little sales history."

    max_bid = snipe = None
    limit_why = ""
    if listing.get("auction"):
        # Keep the bid inside what still counts as a deal (Verdict.is_deal): "good" or better against
        # the same kind's sold median, or for a raw copy, grading under 85% of the PSA 10 median.
        anchors: list[tuple[float, str]] = []
        if cap:
            anchors.append((cap, f"your ceiling of ${cap:,.0f}"))
        # Value limits: a slab's own median; a raw copy's grading value, else its raw median.
        if kind == "psa" and ten:
            anchors.append((ten.median * 0.92, f"92% of the PSA 10 sold median ${ten.median:,.0f}"))
        if kind == "raw":
            # Either route keeps a raw copy a deal, so the more generous one sets the limit.
            routes: list[tuple[float, str]] = []
            if ten and rate:
                routes.append(
                    (
                        ten.median * 0.84 * rate - fee - grade_ship,
                        f"what still beats grading (PSA 10 median ${ten.median:,.0f})",
                    )
                )
            if raw:
                routes.append((raw.median * 0.92, f"92% of the raw sold median ${raw.median:,.0f}"))
            routes = [x for x in routes if math.isfinite(x[0])]
            if routes:
                anchors.append(max(routes))
        anchors = [x for x in anchors if math.isfinite(x[0])]
        if anchors:
            low, why = min(anchors)
            limit = low - (ship or 0)
            max_bid = float(max(0, math.floor(limit)))  # never round up
            snipe = snipe_bid(limit, usd, _bid_count(listing))
            limit_why = f"The lower of {why}" + (f", less ${ship:,.2f} delivery" if ship else "")
            if running and same:
                finish = (
                    f"Auction: likely to finish near ${expected:,.0f} (sold median)"
                    if expected > total
                    else f"Auction: the bid is already at the ${same.median:,.0f} sold median"
                )
            elif running:
                finish = "Auction: no sold median to judge the finish by"
            else:
                finish = "Auction"
            if snipe is not None:
                advice = f"{finish}; bid up to ${snipe:,.2f}." if running else advice
                if not running:
                    advice += f" Auction: max bid ${snipe:,.2f} keeps it a deal."
            else:
                max_bid = None
                limit_why += ". The limit is already below the next minimum bid"
                advice = (
                    f"{finish}; the limit is already below the next minimum bid."
                    if running
                    else advice
                )

    # A running auction is a deal when its likely finish is within the ceiling and a max bid is
    # still possible: a snipe at that bid wins it under the market or loses it for nothing.
    snipeable = running and same is not None and snipe is not None and not held

    star_txt = f"{star}★ " if star != "-" else ""
    headline = f"{w['who']} {star_txt}{'PSA 10' if kind == 'psa' else 'raw'} ${total:,.0f}"
    return Verdict(
        rating,
        headline,
        advice,
        reasons,
        total,
        grade_cost,
        max_bid,
        snipe,
        limit_why,
        expected if running else None,
        held,
        buy_to_grade,
        median=same.median if same else None,
        low=same.low if same else None,
        high=same.high if same else None,
        snipeable=snipeable,
    )
