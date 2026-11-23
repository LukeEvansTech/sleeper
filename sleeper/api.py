"""JSON for the page search and a phone app: the same listings, deal page and wants the web pages show.

Every route sits under /api/v1 and answers with the figures the pages print, so the two never
disagree. Like the pages it has no login: reach is limited by where the app is served.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request

from . import match, photos
from .config import Settings
from .db import DB
from .groups import ordered_sets

# The same two things web.py names: a deal whose photo shows another card stays off the lists,
# and the kv key holding the order the pages list sets in.
SHOWN = "coalesce(json_extract(art, '$.verdict'), '') != 'differs'"
SET_ORDER = "set_order"
RATING_ORDER = {"great": 0, "good": 1, "unknown": 2, "fair": 3, "high": 4}


@dataclass(frozen=True)
class Views:
    """What web.py already works out for its pages, handed over so the API reuses it."""

    listed: Callable[..., dict]
    ended: Callable[[], dict]
    deal: Callable[[str, str], dict]
    block: Callable[[str, str], None]
    scored: Callable[[list[dict]], list[dict]]
    skipped: Callable[[dict], set[str]]


def stamp(local: str | None) -> str | None:
    """A sweep's naive local time with its UTC offset, so a phone in another zone reads it right."""
    if not local:
        return None
    try:
        return dt.datetime.fromisoformat(str(local)).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return None


def image(src: str, size: int) -> str:
    return re.sub(r"s-l\d+\.", f"s-l{size}.", src or "")


def outcome_json(r: dict) -> dict | None:
    o = r.get("outcome")
    if not o:
        return None
    return {
        "status": o["status"],
        "ended": o.get("ended_at"),
        "final": o.get("final_usd"),
        "bids": o.get("bids"),
        "hindsight": r.get("hindsight"),
    }


def listing_json(r: dict, since: str, gixen: Callable[[str, float | None], str]) -> dict[str, Any]:
    """One listing as a tile shows it. Prices are US dollars; `total` is the landed price."""
    v, w = r["v"], r["want"]
    live = bool(r.get("active")) and not r.get("outcome")
    return {
        "item_id": r["item_id"],
        "kind": r["kind"],
        "title": r["title"],
        "want": {"id": w["_id"], "name": w.get("who", ""), "set": w.get("set", "")},
        "star": r.get("star") or "",
        "image": image(r.get("img", ""), 500),
        "price": r["usd"],
        "delivery": r.get("ship"),
        "total": v.total,
        "ceiling": r.get("cap"),
        "rating": v.rating,
        "headline": v.headline,
        "advice": v.advice,
        "deal": bool(r.get("deal")),
        "steal": bool(v.steal),
        "auction": bool(r.get("auction")),
        "offers": bool(r.get("obo")),
        "bids": r.get("bids") or "",
        "time_left": r.get("time_left") or "",
        "ends": r.get("ends"),
        "max_bid": v.snipe if r.get("auction") and live else None,
        "median": v.median,
        "low": v.low,
        "high": v.high,
        "gem": v.gem if r["kind"] == "raw" else None,
        "gem_own": bool(v.gem_own),
        "pop": list(v.pop) if v.pop else None,
        "caution": match.caution(w, r["kind"]),
        "art": (r.get("artd") or {}).get("verdict") or "",
        "first_seen": stamp(r.get("first_seen")),
        "last_seen": stamp(r.get("last_seen")),
        "new": str(r.get("first_seen") or "") >= since,
        "active": bool(r.get("active")),
        "ebay_url": f"https://www.ebay.com/itm/{r['item_id']}",
        "gixen_url": (
            gixen(r["item_id"], r.get("gixen_bid")) if r.get("auction") and live else None
        ),
        "outcome": outcome_json(r),
    }


def add_routes(  # pylint: disable=too-many-locals,too-many-statements
    app: FastAPI,
    db: DB,
    s: Settings,
    views: Views,
    new_since: Callable[[], str],
    new_hours: int,
    gixen: Callable[[str, float | None], str],
) -> None:
    def tile(r: dict, since: str) -> dict:
        return listing_json(r, since, gixen)

    @app.get("/api/search")
    def search() -> dict:
        """What the Cmd-K search filters in the browser: every want and every live deal."""
        counts = {
            r["want_id"]: (r["n"], r["d"] or 0)
            for r in db.all(
                f"SELECT want_id, count(*) AS n, sum(deal AND {SHOWN}) AS d FROM listings WHERE active = 1 "
                "GROUP BY want_id"
            )
        }
        wants_out = []
        for w in db.wants():
            refs = photos.refs_for(w, s.refs_dir)
            kept = [r for r in refs if r.name not in views.skipped(w)] or refs
            n, d = counts.get(w["_id"], (0, 0))
            wants_out.append(
                {
                    "name": w["who"],
                    "set": w.get("set", ""),
                    "href": f"/?want={w['_id']}&show=all",
                    "img": f"/refs/{kept[0].name}" if kept else "",
                    "side": f"{d} deal{'s' if d != 1 else ''}, {n} live",
                }
            )
        deals = views.scored(
            [
                dict(r)
                for r in db.all(f"SELECT * FROM listings WHERE active = 1 AND deal = 1 AND {SHOWN}")
            ]
        )
        deals.sort(key=lambda r: (RATING_ORDER.get(r["v"].rating, 9), r["v"].total))
        deals_out = [
            {
                "name": r["want"]["who"],
                "set": r["want"].get("set", ""),
                "kind": "psa 10" if r["kind"] == "psa" else "raw",
                "href": f"/deal/{r['item_id']}/{r['kind']}",
                "img": image(r["img"], 140),
                "side": f"${r['v'].total:,.2f} {r['v'].rating}",
            }
            for r in deals
        ]
        return {"wants": wants_out, "deals": deals_out}

    @app.get("/api/v1/deals")
    def deals(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        show: str = "deals",
        want: int = 0,
        set: str = "",  # pylint: disable=redefined-builtin
        kind: str = "",
        buy: str = "",
        sort: str = "",
        max: str = "",  # pylint: disable=redefined-builtin
    ) -> dict:
        """Live listings. show: deals, new (deals first seen in the last day) or all. Sort defaults
        to newest on the new view and best elsewhere."""
        got = views.listed(
            show, want, set, kind, buy, sort or ("new" if show == "new" else ""), max
        )
        since = new_since()
        last = db.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        rows = got["rows"]
        return {
            "version": s.version,
            "show": got["show"],
            "sort": got["sort"],
            "kind": got["kind"],
            "buy": got["buy"],
            "max": got["cap"] or None,
            "new_hours": new_hours,
            "total": len(rows),
            "counts": got["counts"],
            "sets": ordered_sets((w.get("set", "") for w in db.wants()), db.get(SET_ORDER, [])),
            "last_sweep": (
                {
                    "status": last["status"],
                    "started": last["started"],
                    "listings": last["listings"],
                }
                if last
                else None
            ),
            "deals": [tile(r, since) for r in rows[:400]],
        }

    @app.get("/api/v1/deals/{item_id}/{kind}")
    def deal(item_id: str, kind: str) -> dict:
        """One listing with its reasons, photos, sold prices and the card's other live listings."""
        c = views.deal(item_id, kind)
        r, since = c["r"], new_since()
        artd = r.get("artd") or {}
        refs = ([artd["ref"]] if artd.get("ref") in c["refs"] else []) + [
            x for x in c["refs"] if x != artd.get("ref")
        ]
        alerted = c["alerted"]
        return tile(r, since) | {
            "reasons": list(r["v"].reasons),
            "limit_why": r["v"].limit_why,
            "expected": r["v"].expected,
            "photo": (f"/photos/{artd['photo']}" if artd.get("photo") else image(r["img"], 1600)),
            "art_score": artd.get("score"),
            "art_print": artd.get("print") or "",
            "refs": [f"/refs/{x}" for x in refs[:3]],
            "lowest": r.get("low_usd"),
            "comps": [
                {
                    "kind": k,
                    "star": star,
                    "median": comp.median,
                    "count": comp.count,
                    "low": comp.low,
                    "high": comp.high,
                    "last": comp.last,
                }
                for k, star, comp in c["card_comps"]
            ],
            "sales": [
                {
                    "date": x["date"],
                    "kind": x["kind"],
                    "star": x["star"],
                    "price": x["usd"],
                }
                for x in c["recent"]
            ],
            "others": [tile(x, since) for x in c["others"]],
            "links": [{"label": label, "url": url} for label, url in c["links"]],
            "alerted": ({"sent": alerted["sent"], "price": alerted["usd"]} if alerted else None),
        }

    @app.post("/api/v1/deals/{item_id}/{kind}/block")
    async def block(item_id: str, kind: str, request: Request) -> dict:
        """Take a listing off every list for good. Body: {"reason": "..."} (optional)."""
        views.deal(item_id, kind)  # 404 for a listing sleeper never saw
        try:
            body = await request.json()
        except ValueError:
            body = {}
        reason = str(body.get("reason") or "not wanted") if isinstance(body, dict) else "not wanted"
        views.block(item_id, reason)
        return {"blocked": item_id}

    @app.get("/api/v1/ended")
    def ended() -> dict:
        """Deals and alerts whose listing has ended, newest first, with how each finished."""
        got = views.ended()
        since = new_since()
        return {
            "total": got["total"],
            "sold": got["sold"],
            "tally": got["tally"],
            "deals": [tile(r, since) for r in got["rows"]],
        }

    @app.get("/api/v1/wants")
    def wants() -> dict:
        """Every want with its live listing and deal counts, by set."""
        counts = {
            r["want_id"]: (r["n"], r["d"] or 0)
            for r in db.all(
                f"SELECT want_id, count(*) AS n, sum(deal AND {SHOWN}) AS d FROM listings "
                "WHERE active = 1 GROUP BY want_id"
            )
        }
        by_set: dict[str, list[dict]] = {}
        for w in db.wants():
            refs = photos.refs_for(w, s.refs_dir)
            kept = [r for r in refs if r.name not in views.skipped(w)] or refs
            n, d = counts.get(w["_id"], (0, 0))
            by_set.setdefault(w.get("set", ""), []).append(
                {
                    "id": w["_id"],
                    "name": w.get("who") or w.get("q", ""),
                    "set": w.get("set", ""),
                    "cards": w.get("cards") or "",
                    "enabled": bool(w.get("_enabled")),
                    "image": f"/refs/{kept[0].name}" if kept else "",
                    "live": n,
                    "deals": d,
                    "ceilings": w.get("max") or {},
                }
            )
        order = ordered_sets(by_set, db.get(SET_ORDER, []))
        return {
            "total": sum(len(v) for v in by_set.values()),
            "sets": [
                {
                    "name": k,
                    "wants": sorted(v, key=lambda w: (not w["enabled"], w["name"].lower())),
                }
                for k, v in ((x, by_set[x]) for x in order if x in by_set)
            ],
        }
