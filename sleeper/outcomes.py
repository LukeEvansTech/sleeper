"""How listings ended: the final price, bid count and whether they sold.

The Browse API keeps returning a listing after it ends, so each sweep asks about the listings whose
sleeper row stopped being live. A sale becomes a row in `sold`, which grows the comps without the Mac.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from . import match
from .db import DB

log = logging.getLogger(__name__)

MAX_CHECKS = 200  # Browse calls per sweep; the rest wait for the next one
RECHECK_HOURS = 20  # a listing still on sale is asked about again after this long
GIVE_UP_DAYS = 30  # and not at all this long after the first check
STOP_AFTER_FAILURES = 5  # consecutive failed calls: eBay is down, leave the rest for later
FINAL = ("sold", "unsold")


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse(item: dict, now: dt.datetime | None = None) -> dict:
    """An item as eBay returns it -> the outcome row (without its timestamps)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    avail: dict = next(iter(item.get("estimatedAvailabilities") or []), {}) or {}
    price = item.get("price") or {}
    # An auction's winning bid is currentBidPrice; its `price` may be the Buy It Now amount.
    final = None
    if "AUCTION" in (item.get("buyingOptions") or []):
        final = _num((item.get("currentBidPrice") or {}).get("value"))
    if final is None:
        final = _num(price.get("value"))
    cur = price.get("convertedFromCurrency") or price.get("currency") or ""
    end = None
    try:
        end = dt.datetime.fromisoformat((item.get("itemEndDate") or "").replace("Z", "+00:00"))
    except ValueError:
        pass
    out_of_stock = avail.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK"
    # A sold-out listing with no end date may be sold-out stock the seller will refill, so only an
    # auction, or a listing whose end date has passed, is over.
    auction = "AUCTION" in (item.get("buyingOptions") or [])
    ended = (end is not None and end <= now) or (out_of_stock and (auction or end is not None))
    n_bids = _num(item.get("bidCount"))
    bids = int(n_bids) if n_bids is not None else None
    n_sold = _num(avail.get("estimatedSoldQuantity"))
    sold_qty = int(n_sold) if n_sold is not None else None
    if sold_qty is None and auction:
        sold_qty = 1 if (bids or 0) > 0 else 0  # an auction's sold count is missing: bids decide
    if item.get("reservePriceMet") is False:  # bids that never met the reserve are not a sale
        sold_qty = 0
    if ended and sold_qty is None:
        ended = False  # no sales count at all: unresolved, not "unsold"
    sold = ended and (sold_qty or 0) >= 1
    if sold and not (final and final > 0):
        ended = sold = False  # a sale with no usable price is an incomplete answer: ask again
    return {
        "final_usd": final,
        "currency": cur,
        "bids": bids,
        "sold_qty": sold_qty,
        "status": "sold" if sold else "unsold" if ended else "on_sale",
        "ended_at": end.isoformat(timespec="minutes") if end else None,
    }


def candidates(db: DB, now: dt.datetime, limit: int = MAX_CHECKS) -> list[dict]:
    """Listings to ask eBay about: not live (or an auction past its end) and not finally recorded.
    Never-asked first, then the longest since asked."""
    due = (now - dt.timedelta(hours=RECHECK_HOURS)).isoformat(timespec="seconds")
    stop = (now - dt.timedelta(days=GIVE_UP_DAYS)).isoformat(timespec="seconds")
    rows = db.all(
        """SELECT l.item_id, l.kind, l.want_id, l.star, l.title, l.obo, l.auction, l.art, o.checked_at
           FROM listings l LEFT JOIN outcomes o ON o.item_id = l.item_id
           WHERE (l.active = 0 OR (l.auction = 1 AND l.ends IS NOT NULL AND l.ends < ?))
             AND (o.item_id IS NULL
                  OR (o.status NOT IN ('sold', 'unsold') AND o.checked_at < ? AND o.first_checked_at > ?))
           ORDER BY o.checked_at IS NOT NULL, o.checked_at, l.item_id""",
        now.astimezone().replace(tzinfo=None).isoformat(timespec="minutes"),
        due,
        stop,
    )
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["item_id"] not in seen:  # one call per item, whichever kind row comes first
            seen.add(r["item_id"])
            out.append(dict(r))
    return out[:limit]


def record(db: DB, cand: dict, got: dict | None, now: dt.datetime) -> str:
    """Store one answer (None = the call failed) and, for a sale, add it to the sold comps."""
    stamp = now.isoformat(timespec="seconds")
    o = got or {"final_usd": None, "currency": "", "bids": None, "sold_qty": None,
                "status": "error", "ended_at": None}  # fmt: skip
    with db.tx() as c:
        c.execute(
            """INSERT INTO outcomes (item_id, final_usd, currency, bids, sold_qty, status, ended_at,
                   checked_at, first_checked_at)
               VALUES (:item_id, :final_usd, :currency, :bids, :sold_qty, :status, :ended_at, :now, :now)
               ON CONFLICT(item_id) DO UPDATE SET final_usd = excluded.final_usd,
                   currency = excluded.currency, bids = excluded.bids, sold_qty = excluded.sold_qty,
                   status = excluded.status, ended_at = coalesce(excluded.ended_at, outcomes.ended_at),
                   checked_at = excluded.checked_at""",
            {**o, "item_id": cand["item_id"], "now": stamp},
        )
        if o["status"] in FINAL:  # over: never live, rated or alerted again
            c.execute("UPDATE listings SET active = 0 WHERE item_id = ?", (cand["item_id"],))
        # Only an auction's final price is a price someone paid: a fixed-price listing's sold count
        # is cumulative at no known price (offers, earlier prices), and a rejected listing (blocked,
        # wrong slab, another card's photo) is not this card.
        rejected = c.execute(
            "SELECT 1 FROM blocked WHERE item_id = ?", (cand["item_id"],)
        ).fetchone() or (json.loads(cand["art"]) if cand["art"] else {}).get("verdict") in (
            "differs",
            "unwanted",
        )
        if o["status"] == "sold" and cand["auction"] and not rejected:
            w = c.execute("SELECT id, spec FROM wants WHERE id = ?", (cand["want_id"],)).fetchone()
            if w:
                spec = json.loads(w["spec"])
                spec["_id"] = w["id"]
                c.execute(
                    "INSERT OR IGNORE INTO sold (item_id, want_id, kind, star, usd, date, title) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        cand["item_id"],
                        match.comp_id(spec),
                        cand["kind"],
                        cand["star"],
                        o["final_usd"],  # the item price only, as the sold search rows carry it
                        (o["ended_at"] or stamp)[:10],
                        cand["title"][:200],
                    ),
                )
    return str(o["status"])


def check(db: DB, source: Any, now: dt.datetime | None = None, limit: int = MAX_CHECKS) -> dict:
    """Ask eBay about each candidate listing. Never raises: a failed call just waits for next time."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stats = {"asked": 0, "sold": 0, "unsold": 0, "on_sale": 0, "error": 0}
    ask = getattr(source, "item", None)
    if ask is None:  # the headless browser cannot look an item up
        return stats
    failures = 0
    try:
        for cand in candidates(db, now, limit):
            try:
                item = ask(cand["item_id"])
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warning("outcome lookup failed: %s", type(e).__name__)
                item = None
            stats["asked"] += 1
            status = record(db, cand, parse(item, now) if item else None, now)
            stats[status] += 1
            failures = failures + 1 if status == "error" else 0
            if failures >= STOP_AFTER_FAILURES:
                break
    except Exception:  # pylint: disable=broad-exception-caught
        log.exception("outcome check failed")
    return stats
