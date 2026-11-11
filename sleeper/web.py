"""Web UI and API. Every alert links to /deal/<item>/<kind>; a digest links to /run/<id>."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import match, notify, photos, summary, sweep, value
from .config import Settings
from .db import DB

HERE = Path(__file__).resolve().parent
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
RATING_ORDER = {"great": 0, "good": 1, "unknown": 2, "fair": 3, "high": 4}
# A deal whose photo shows another card stays off the deals list, as it stays out of alerts.
SHOWN = "coalesce(json_extract(art, '$.verdict'), '') != 'differs'"


def money(v) -> str:
    try:  # imported settings may hold numeric strings
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(f):
        return ""
    return f"${f:,.2f}" if f < 100 else f"${f:,.0f}"


def bid(v) -> str:
    """A bid to the cent: the amount is chosen to the cent, so never round it."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return f"${f:,.2f}" if math.isfinite(f) else ""


def star_label(star: str) -> str:
    return f"{star}★" if star and star != "-" else ""


def ebay_img(src: str, size: int = 500) -> str:
    return re.sub(r"s-l\d+\.", f"s-l{size}.", src or "")


def buy_links(w: dict) -> list[tuple[str, str]]:
    """Other places to look for this card. eBay first (US view), then the shops Luke uses."""
    psa_q = match.searches(w)[0][1] if match.searches(w) else w["who"]
    plain = re.sub(r"\(.*?\)|-\S+|\bpsa 10\b", " ", psa_q)
    plain = re.sub(r"\s+", " ", plain).strip() or w["who"]
    enc = urllib.parse.quote_plus
    return [
        ("eBay: live PSA 10s", match.search_url(psa_q)),
        ("eBay: sold PSA 10s", match.search_url(psa_q, sold=True)),
        ("TCGplayer (raw)", f"https://www.tcgplayer.com/search/all/product?q={enc(plain)}"),
        (
            "GameStop (PSA slabs)",
            f"https://www.gamestop.com/search/?q={enc(plain + ' psa 10')}",
        ),
        ("Mercari US", f"https://www.mercari.com/search/?keyword={enc(plain)}"),
        ("PWCC / Fanatics Collect", f"https://www.fanaticscollect.com/search?q={enc(plain)}"),
    ]


def ends_at(r: dict) -> float:
    """Auction end as a timestamp; listings without one (not auctions, or not seen since) go last."""
    if not r.get("auction") or not r.get("ends"):
        return float("inf")
    return dt.datetime.fromisoformat(r["ends"]).timestamp()


BUY_FILTERS: dict[str, Callable[[dict], bool]] = {
    "": lambda r: True,
    "bin": lambda r: not r["auction"],
    "auction": lambda r: bool(r["auction"]),
    "offers": lambda r: bool(r["obo"]),
}
SORTS: dict[str, Callable[[dict], Any]] = {
    "deal": lambda r: (not r["deal"], RATING_ORDER.get(r["v"].rating, 9), r["v"].total),
    "price": lambda r: r["v"].total,
    "ending": lambda r: (ends_at(r), r["v"].total),
    "new": lambda r: -dt.datetime.fromisoformat(r["first_seen"]).timestamp(),
}


def snipe_bid(r: dict) -> float | None:
    """The bid a Gixen snipe defaults to: the verdict's max bid (value.snipe_bid). None when there is
    no bid that keeps it a deal, or it is below the next minimum bid."""
    best = r["v"].snipe
    return float(best) if best and best > 0 else None


def gixen_link(item_id: str, bid: float | None) -> str:
    """Gixen's add-snipe form with the item (and bid) filled in; Luke signs in and presses Add."""
    q = {"itemid": item_id} | ({"maxbid": f"{bid:.2f}"} if bid else {})
    return "https://www.gixen.com/autosnipe.php?" + urllib.parse.urlencode(q)


def create_app(s: Settings, db: DB | None = None, start_scheduler: bool = False) -> FastAPI:
    db = db or DB(s.db_path)
    app = FastAPI(title="sleeper", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        """Refuse a browser write that another site's page sent (no login here, so no CSRF token)."""
        if request.method in ("POST", "PUT", "DELETE"):
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            host = urllib.parse.urlsplit(origin).netloc
            if origin and host != request.headers.get("host", ""):
                return PlainTextResponse("cross-site request refused", status_code=403)
        return await call_next(request)

    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.filters["money"] = money
    tpl.env.filters["bid"] = bid
    tpl.env.filters["star"] = star_label
    tpl.env.filters["ebay_img"] = ebay_img
    tpl.env.filters["pct"] = pct
    tpl.env.globals["gixen_link"] = gixen_link
    tpl.env.globals["landed"] = lambda r, usd: usd + (r.get("ship") or 0)
    tpl.env.globals["version"] = s.version

    def when(iso: str) -> str:
        """An auction end in the app's timezone: '14:05 today', 'Fri 09:10'."""
        at = dt.datetime.fromisoformat(iso).astimezone(ZoneInfo(s.timezone))
        today = dt.datetime.now(ZoneInfo(s.timezone)).date()
        if at.date() == today:
            return f"{at:%H:%M} today"
        return f"{at:%a %H:%M}" if (at.date() - today).days < 6 else f"{at:%a %-d %b %H:%M}"

    tpl.env.filters["when"] = when
    tpl.env.filters["day"] = lambda at: f"{at.astimezone(ZoneInfo(s.timezone)):%A %-d %B}"
    progress: list[str] = []

    def art_choices(w: dict) -> list[dict]:
        skip = set_of(w.get("skip"))
        rarity = {**db.get("ref_labels", {}), **photos.listed_rarity(w)}
        return [
            {"name": r.name, "label": photos.label(r.name, rarity), "kept": r.name not in skip}
            for r in photos.refs_for(w, s.refs_dir)
        ]

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        return tpl.TemplateResponse(request, name, ctx)

    def start_sweep(trigger: str, want_ids: list[int] | None = None) -> bool:
        if sweep.LOCK.locked():
            return False
        progress.clear()

        def say(m: str) -> None:
            progress.append(f"{dt.datetime.now():%H:%M:%S} {m}")
            del progress[:-300]

        threading.Thread(
            target=sweep.run,
            kwargs={"db": db, "s": s, "trigger": trigger, "want_ids": want_ids, "progress": say},
            daemon=True,
        ).start()
        return True

    def render_summary(g: dict, img: Callable[[dict], str]) -> tuple[str, str]:
        """The summary email as (html, plain text)."""
        title = summary.subject(g)
        html = tpl.get_template("summary_email.html").render(
            g=g, img=img, link=s.link, cap=summary.MAX_CARDS, title=title
        )
        lines = [title, ""]
        for head, rows in (
            ("Auctions ending in the next 24 hours", g["ending"]),
            ("New graded deals", g["new_graded"]),
            ("New raw deals", g["new_raw"]),
        ):
            if rows:
                lines.append(head)
                for r in rows[: summary.MAX_CARDS]:
                    lines.append(
                        f"- {r['want']['who']}: {money(r['v'].total)} {r['v'].rating}"
                        f"{', max bid ' + bid(r['v'].snipe) if r['auction'] and r['v'].snipe else ''} "
                        f"{s.link('/deal/' + r['item_id'] + '/' + r['kind'])}"
                    )
                lines.append("")
        lines.append(f"{g['live']} deals live. All deals: {s.link('/')}")
        if g["runs"] == 0:
            lines.append("No sweeps ran since the last summary.")
        elif g["bad_runs"]:
            lines.append(f"{g['bad_runs']} of {g['runs']} sweeps had a problem: {s.link('/ops')}")
        if g["failed_searches"]:
            lines.append(f"{g['failed_searches']} of {g['searches']:,} searches failed.")
        return html, "\n".join(lines)

    if start_scheduler:
        from . import scheduler

        scheduler.start(s.schedule, s.timezone, lambda: start_sweep("schedule"))
        if s.summary_on:
            scheduler.start(
                s.summary_schedule,
                s.timezone,
                lambda: summary.run(db, s, render_summary),
                name="summary email",
            )

    def scored(rows: list[dict]) -> list[dict]:
        wants = {w["_id"]: w for w in db.wants()}
        cmp, pop = sweep.context(db)
        sold_rows: list[dict] | None = None  # loaded only when a row has ended
        outs = {
            o["item_id"]: dict(o)
            for o in db.all("SELECT * FROM outcomes WHERE status IN ('sold', 'unsold')")
        }
        out = []
        for r in rows:
            w = wants.get(r["want_id"])
            if not w:
                continue
            r = dict(r)
            r["outcome"] = outs.get(r["item_id"])
            r["want"] = w
            r["v"] = sweep.verdict_for(db, s, r, w, cmp, pop)
            if r["outcome"] and r.get("auction"):
                if sold_rows is None:
                    sold_rows = [dict(x) for x in db.all("SELECT * FROM sold")]
                r["hindsight"] = hindsight(r, w, sold_rows, pop)
            r["artd"] = json.loads(r["art"]) if r.get("art") else {}
            r["bid_to"] = snipe_bid(r)
            # Gixen bids in the listing's own currency, so a dollar figure goes only on USD listings.
            r["gixen_bid"] = r["bid_to"] if r.get("cur") == "USD" else None
            out.append(r)
        return out

    def hindsight(r: dict, w: dict, sold_rows: list[dict], pop: list[dict]) -> dict:
        """Would sleeper's max bid have won this ended auction? The max bid is recalculated for a
        minute before the end from sales on earlier days only (sold rows carry a date, not a time),
        so the result it judges never feeds it. The ceiling is the one the listing was stored with;
        pop and grading figures are today's. The winner's own maximum is hidden, so a max
        bid over the final price is "in range" (it beat every other bidder), never a sure win."""
        o = r["outcome"]
        end = o.get("ended_at") or r.get("ends")
        out: dict = {"comp": any(x["item_id"] == r["item_id"] for x in sold_rows)}
        try:
            at = dt.datetime.fromisoformat(str(end)[:16])
        except ValueError:
            return out | {"result": "unknown"}
        prior = [
            x
            for x in sold_rows
            if x["item_id"] != r["item_id"] and x["date"][:10] < at.date().isoformat()
        ]
        v = value.assess(
            r,
            w,
            value.comps(prior, at.date()),
            value.pop_for(pop, w),
            db.gem_rates(),
            s.grading_fee,
            s.grading_ship,
            now=at - dt.timedelta(minutes=1),
        )
        limit = v.limit
        bid = value.snipe_bid(limit) if limit is not None else None
        out["bid"] = bid
        final, bids = o.get("final_usd"), o.get("bids")
        # The bid sleeper last saw already sets a floor: assess() gives no snipe when the limit is
        # under the next bid eBay would take then, so that max bid could never have been placed.
        placeable = v.snipe is not None
        if bid is None:
            out["result"] = "no bid"  # no ceiling and no sales to set a limit
        elif bids is None:
            out["result"] = "unknown"  # eBay gave no bid count
        elif not placeable:
            out["result"] = "lost"
        elif o["status"] == "unsold" and bids:
            out["result"] = "reserve"  # bids came in but the reserve was not met
        elif o["status"] == "unsold":
            # Nobody bid: the opening price would probably have taken it, unless a hidden reserve.
            start = float(r["usd"])
            out["result"], out["pay"] = "open", start
        elif final is None or bids == 0:
            out["result"] = "unknown"  # sold by offer or Buy It Now: no closing price
        elif bid > final:
            # It beat every bid but the winner's, whose maximum eBay never shows, and bid timing is
            # unknown, so this is a chance at it costing more than the final price, not a sure win.
            out["result"], out["pay"] = "range", final
        else:
            out["result"] = "lost"
        return out

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": s.version}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        last = db.one(
            "SELECT strftime('%s', finished) AS t FROM runs WHERE status = 'success' AND searches > 0 "
            "AND alerts NOT LIKE 'failed%' AND alerts NOT LIKE '%failed:%' ORDER BY id DESC LIMIT 1"
        )
        live = db.one(
            f"SELECT count(*) AS n FROM listings WHERE active = 1 AND deal = 1 AND {SHOWN}"
        )
        wants = db.one("SELECT count(*) AS n FROM wants WHERE enabled = 1")
        return (
            "# HELP sleeper_last_success_timestamp_seconds End of the last sweep that searched "
            "and alerted without a failure.\n# TYPE sleeper_last_success_timestamp_seconds gauge\n"
            f"sleeper_last_success_timestamp_seconds {int(last['t']) if last and last['t'] else 0}\n"
            "# TYPE sleeper_run_in_progress gauge\n"
            f"sleeper_run_in_progress {int(sweep.LOCK.locked())}\n"
            "# TYPE sleeper_live_deals gauge\n"
            f"sleeper_live_deals {live['n'] if live else 0}\n"
            "# TYPE sleeper_wants gauge\n"
            f"sleeper_wants {wants['n'] if wants else 0}\n"
        )

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        set: str = "",
        kind: str = "",
        show: str = "deals",
        want: int = 0,
        buy: str = "",
        sort: str = "deal",
    ):
        sql = "SELECT * FROM listings WHERE active = 1"
        args: list = []
        cur_want = db.want(want) if want else None
        if want and not cur_want:
            raise HTTPException(404, "want not found")
        if cur_want:
            sql += " AND want_id = ?"
            args.append(want)
        if show == "deals":
            sql += f" AND deal = 1 AND {SHOWN}"
        rows = scored([dict(r) for r in db.all(sql, *args)])
        if set:
            rows = [r for r in rows if r["want"].get("set") == set]
        kind = kind if kind in ("psa", "raw") else ""
        buy = buy if buy in BUY_FILTERS else ""
        sort = sort if sort in SORTS else "deal"
        # each chip counts what it would show with the other filter kept
        counts = {
            "kind": {
                k: sum(1 for r in rows if (not k or r["kind"] == k) and BUY_FILTERS[buy](r))
                for k in ("", "psa", "raw")
            },
            "buy": {
                b: sum(1 for r in rows if (not kind or r["kind"] == kind) and f(r))
                for b, f in BUY_FILTERS.items()
            },
        }
        rows = [r for r in rows if (not kind or r["kind"] == kind) and BUY_FILTERS[buy](r)]
        rows.sort(key=SORTS[sort])
        sets = sorted({w.get("set", "") for w in db.wants()})
        last = db.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        return page(
            request,
            "home.html",
            rows=rows[:400],
            total=len(rows),
            sets=sets,
            cur_set=set,
            kind=kind,
            buy=buy,
            sort=sort,
            counts=counts,
            show=show,
            cur_want=cur_want,
            last=last,
            running=sweep.LOCK.locked(),
        )

    @app.get("/ended", response_class=HTMLResponse)
    def ended(request: Request):
        """Deals and alerts whose listing has since ended, newest first, with how each finished."""
        rows = scored(
            [
                dict(r)
                for r in db.all(
                    """SELECT l.* FROM listings l JOIN outcomes o ON o.item_id = l.item_id
                        WHERE o.status IN ('sold', 'unsold') AND coalesce(json_extract(l.art, '$.verdict'), '') != 'differs'
                          AND (l.deal = 1 OR EXISTS (SELECT 1 FROM alerts a
                                 WHERE a.item_id = l.item_id AND a.kind = l.kind))
                        ORDER BY coalesce(o.ended_at, o.checked_at) DESC, l.item_id"""
                )
            ]
        )
        sold = sum(1 for r in rows if r["outcome"]["status"] == "sold")
        tally = {
            k: sum(1 for r in rows if (r.get("hindsight") or {}).get("result") == k)
            for k in ("open", "range", "lost")
        }
        return page(request, "ended.html", rows=rows[:400], total=len(rows), sold=sold, tally=tally)

    @app.get("/deal/{item_id}/{kind}", response_class=HTMLResponse)
    def deal(request: Request, item_id: str, kind: str):
        row = db.one("SELECT * FROM listings WHERE item_id = ? AND kind = ?", item_id, kind)
        if not row:
            raise HTTPException(404, "listing not found")
        found = scored([dict(row)])
        if not found:
            raise HTTPException(404, "its want was deleted")
        r = found[0]
        w = r["want"]
        cmp, pop = sweep.context(db)
        card_comps = sorted(
            [(k[1], k[2], c) for k, c in cmp.items() if k[0] == match.comp_id(w)],
            key=lambda x: (x[0], x[1]),
        )
        recent = db.all(
            "SELECT * FROM sold WHERE want_id = ? ORDER BY date DESC LIMIT 12", match.comp_id(w)
        )
        others = scored(
            [
                dict(x)
                for x in db.all(
                    "SELECT * FROM listings WHERE want_id = ? AND active = 1 AND NOT "
                    "(item_id = ? AND kind = ?) ORDER BY usd LIMIT 8",
                    w["_id"],
                    item_id,
                    kind,
                )
            ]
        )
        refs = [p.name for p in photos.refs_for(w, s.refs_dir)]
        return page(
            request,
            "deal.html",
            r=r,
            w=w,
            card_comps=card_comps,
            recent=recent,
            others=others,
            pop=value.pop_for(pop, w)[:10],
            refs=refs,
            links=buy_links(w),
            alerted=db.one("SELECT * FROM alerts WHERE item_id = ? AND kind = ?", item_id, kind),
        )

    @app.post("/deal/{item_id}/{kind}/block")
    def block(item_id: str, kind: str, reason: str = Form("not wanted")):
        db.run(
            "INSERT OR REPLACE INTO blocked (item_id, reason) VALUES (?, ?)", item_id, reason[:200]
        )
        db.run("UPDATE listings SET active = 0 WHERE item_id = ?", item_id)
        return RedirectResponse(f"/deal/{item_id}/{kind}", status_code=303)

    @app.get("/run/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int):
        run = db.one("SELECT * FROM runs WHERE id = ?", run_id)
        if not run:
            raise HTTPException(404, "run not found")
        rows = scored(
            [
                dict(r)
                for r in db.all(
                    "SELECT l.* FROM listings l JOIN run_alerts a ON a.item_id = l.item_id "
                    "AND a.kind = l.kind WHERE a.run_id = ?",
                    run_id,
                )
            ]
        )
        if not rows:
            rows = scored(
                [
                    dict(r)
                    for r in db.all(
                        "SELECT * FROM listings WHERE last_run = ? AND deal = 1", run_id
                    )
                ]
            )
        rows.sort(key=lambda r: (RATING_ORDER.get(r["v"].rating, 9), r["v"].total))
        return page(
            request,
            "run.html",
            run=run,
            rows=rows,
            failed=json.loads(run["failed"] or "[]"),
        )

    # --- wants ---
    @app.get("/wants", response_class=HTMLResponse)
    def wants_page(request: Request, set: str = "", view: str = "cards"):
        wants = db.wants()
        counts = {
            (r["want_id"]): (r["n"], r["d"])
            for r in db.all(
                f"SELECT want_id, count(*) AS n, sum(deal AND {SHOWN}) AS d FROM listings WHERE active = 1 "
                "GROUP BY want_id"
            )
        }
        if set:
            wants = [w for w in wants if w.get("set") == set]
        sets = sorted({w.get("set", "") for w in db.wants()})
        thumbs = {}
        for w in wants:
            refs = photos.refs_for(w, s.refs_dir)
            kept = [r for r in refs if r.name not in set_of(w.get("skip"))]
            if refs:
                thumbs[w["_id"]] = {
                    "name": (kept or refs)[0].name,
                    "kept": len(kept),
                    "total": len(refs),
                }
        return page(
            request,
            "wants.html",
            wants=wants,
            counts=counts,
            sets=sets,
            cur_set=set,
            view="table" if view == "table" else "cards",
            thumbs=thumbs,
            defaults=db.defaults(),
        )

    @app.post("/wants/defaults")
    async def wants_defaults(request: Request):
        form = await request.form()
        cur = db.defaults()
        for key in ("psa_1star_max", "raw_1star_max"):
            raw = str(form.get(key, "")).strip().lstrip("$").replace(",", "")
            try:
                val = float(raw)
            except ValueError as e:
                raise HTTPException(400, f"{key} must be a number") from e
            if not math.isfinite(val) or val < 0:
                raise HTTPException(400, f"{key} must be a number of zero or more")
            cur[key] = val
        db.put("defaults", cur)
        return RedirectResponse("/wants", status_code=303)

    @app.get("/wants/new", response_class=HTMLResponse)
    def want_new(request: Request):
        return page(
            request, "want_form.html", w={"set": "", "who": ""}, extra="{}", error="", art=[]
        )

    @app.get("/wants/{want_id}", response_class=HTMLResponse)
    def want_edit(request: Request, want_id: int):
        w = db.want(want_id)
        if not w:
            raise HTTPException(404, "want not found")
        extra = {k: v for k, v in w.items() if k not in FORM_KEYS and not k.startswith("_")}
        return page(
            request,
            "want_form.html",
            w=w,
            extra=json.dumps(extra, indent=1, ensure_ascii=False),
            error="",
            art=art_choices(w),
        )

    @app.post("/wants")
    async def want_save(request: Request):
        data = await request.form()
        form: dict = dict(data)
        form["keep"] = [str(x) for x in data.getlist("keep")]
        form["art_all"] = [str(x) for x in data.getlist("art_all")]
        want_id = int(str(form["_id"])) if form.get("_id") else None
        old = db.want(want_id) if want_id is not None else None
        form["prev_skip"] = sorted(set_of((old or {}).get("skip")))
        try:
            spec = spec_from_form(form)
        except ValueError as e:
            echo = form_echo(form, want_id)
            return page(
                request,
                "want_form.html",
                w=echo,
                extra=form.get("extra", "{}"),
                error=str(e),
                art=art_choices(echo),
            )
        # An unticked checkbox is simply absent from the form.
        new_id = db.save_want(spec, want_id, enabled=form.get("enabled") == "on")
        # Its refs may have changed: photo verdicts are judged again on the next sweep.
        saved = db.want(new_id)
        fam = match.comp_id(saved) if saved else new_id
        family = [x["_id"] for x in db.wants() if match.comp_id(x) == fam]
        for fid in family or [new_id]:  # a sibling's photo may now belong to this print, or not
            db.run("UPDATE listings SET art = NULL WHERE want_id = ?", fid)
        if saved:
            sweep.retire_unsearched(db, saved)
        return RedirectResponse(f"/wants/{new_id}", status_code=303)

    @app.post("/wants/{want_id}/delete")
    def want_delete(want_id: int):
        gone = db.want(want_id)
        rest = [x for x in db.wants() if x["_id"] != want_id]
        # sold comps live under the card's family id; drop them once no remaining print uses them
        orphans = {
            cid
            for cid in (want_id, match.comp_id(gone) if gone else want_id)
            if not any(match.comp_id(x) == cid for x in rest)
        }
        with db.tx() as c:
            c.execute(
                "DELETE FROM alerts WHERE item_id IN (SELECT item_id FROM listings WHERE want_id = ?)",
                (want_id,),
            )
            c.execute("DELETE FROM listings WHERE want_id = ?", (want_id,))
            c.execute("DELETE FROM wants WHERE id = ?", (want_id,))
            for cid in orphans:
                c.execute("DELETE FROM sold WHERE want_id = ?", (cid,))
        return RedirectResponse("/wants", status_code=303)

    @app.post("/wants/{want_id}/sweep")
    def want_sweep(want_id: int):
        w = db.want(want_id)
        if not w:
            raise HTTPException(404, "want not found")
        start_sweep("manual", want_ids=[want_id])
        return RedirectResponse("/ops", status_code=303)

    # --- ops ---
    @app.get("/ops", response_class=HTMLResponse)
    def ops(request: Request):
        runs = db.all("SELECT * FROM runs ORDER BY id DESC LIMIT 50")
        from . import scheduler

        return page(
            request,
            "ops.html",
            runs=runs,
            running=sweep.LOCK.locked(),
            log=list(progress),
            schedule=s.schedule,
            next_at=scheduler.next_time(s.schedule, s.timezone),
            channels=len([u for u in s.apprise_urls.split(",") if u.strip()]),
            public_url=s.public_url,
            defaults=db.defaults(),
            gem_rates=db.gem_rates(),
        )

    @app.get("/summary", response_class=HTMLResponse)
    def summary_page(request: Request):
        from . import scheduler

        g = summary.gather(db, s)
        return page(
            request,
            "summary.html",
            g=g,
            img=lambda r: ebay_img(r["img"], 225),
            link=s.link,
            cap=summary.MAX_CARDS,
            mail_on=s.summary_on,
            to=s.summary_to,
            next_at=scheduler.next_time(s.summary_schedule, s.timezone),
            last_result=db.get("summary_last_result", ""),
        )

    @app.get("/summary/last", response_class=HTMLResponse)
    def summary_last():
        html = db.get(summary.LAST_HTML, "")
        if not html:
            raise HTTPException(404, "no summary has been sent yet")
        return HTMLResponse(html)

    @app.post("/summary/send")
    def summary_send():
        if not s.summary_on:
            raise HTTPException(409, "the summary email is not configured")
        summary.run(db, s, render_summary)
        return RedirectResponse("/summary", status_code=303)

    @app.get("/ops/log", response_class=PlainTextResponse)
    def ops_log() -> str:
        return "\n".join(progress)

    @app.post("/ops/run")
    def ops_run():
        if not start_sweep("manual"):
            raise HTTPException(409, "a sweep is already running")
        return RedirectResponse("/ops", status_code=303)

    @app.post("/ops/test-alert")
    def ops_test():
        res = notify.send(
            s.apprise_urls,
            "sleeper test alert",
            "Tap to open the deal list.",
            s.link("/"),
            "Open sleeper",
        )
        return PlainTextResponse(res)

    # --- import, images ---
    @app.get("/api/search")
    def api_search() -> dict:
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
            kept = [r for r in refs if r.name not in set_of(w.get("skip"))] or refs
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
        deals = scored(
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
                "img": ebay_img(r["img"], 140),
                "side": f"${r['v'].total:,.2f} {r['v'].rating}",
            }
            for r in deals
        ]
        return {"wants": wants_out, "deals": deals_out}

    @app.post("/api/import")
    async def api_import(request: Request) -> dict:
        """Load a wantwatch export: {wants, defaults, block, gem_rates, pop: {fetched, rows}, sold}."""
        return sweep.load_export(db, await request.json())

    @app.post("/api/sold")
    async def api_sold(request: Request) -> dict:
        body = await request.json()
        return {"added": sweep.import_sold(db, body.get("sold", {}))}

    @app.put("/api/refs/{name}")
    async def api_ref(name: str, request: Request) -> dict:
        if not SAFE_NAME.match(name) or not name.lower().endswith((".jpg", ".jpeg", ".png")):
            raise HTTPException(400, "bad file name")
        data = await request.body()
        if not 1000 < len(data) < 15_000_000:
            raise HTTPException(400, "image size out of range")
        (s.refs_dir / name).write_bytes(data)
        db.run("UPDATE listings SET art = NULL WHERE art IS NOT NULL")  # judge again, matches too
        return {"stored": name, "bytes": len(data)}

    @app.get("/api/refs")
    def api_refs() -> list[str]:
        return sorted(p.name for p in s.refs_dir.iterdir())

    @app.get("/refs/{name}")
    def ref_file(name: str):
        return _file(s.refs_dir, name)

    @app.get("/photos/{name}")
    def photo_file(name: str):
        return _file(s.photos_dir, name)

    return app


def _file(base: Path, name: str) -> FileResponse:
    if not SAFE_NAME.match(name) or not (base / name).is_file():
        raise HTTPException(404, "no such image")
    return FileResponse(base / name, headers={"Cache-Control": "max-age=86400"})


def pct(v) -> str:
    """A rate as a percentage, tolerating the strings and nulls /api/import accepts."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "not set"
    return f"{f:.0%}" if math.isfinite(f) else "not set"


def form_echo(form: dict, want_id: int | None) -> dict:
    """The submitted form in the shape the template reads, so a validation error loses nothing."""
    w: dict = {k: str(v) for k, v in form.items() if isinstance(v, str) and not k.startswith("max")}
    for key, prefix in (("max", "max_"), ("rawmax", "rawmax_")):
        w[key] = {b: str(form[prefix + b]) for b in ("1", "2", "3", "any") if form.get(prefix + b)}
    w["refs"] = [r.strip() for r in str(form.get("refs", "")).split(",") if r.strip()]
    w["_enabled"] = form.get("enabled") == "on"
    keep = set(form.get("keep") or [])
    w["skip"] = [a for a in form.get("art_all") or [] if a not in keep]
    if want_id is not None:
        w["_id"] = want_id
    return w


FORM_KEYS = {
    "set",
    "who",
    "q",
    "rawq",
    "need",
    "id",
    "max",
    "rawmax",
    "refs",
    "cards",
    "notes",
    "skip",
}


def set_of(v) -> set[str]:
    return {str(x) for x in v} if isinstance(v, list) else set()


def spec_from_form(form: dict) -> dict:
    """Build a want from the form; the Advanced JSON box carries every other key."""
    try:
        spec = json.loads(form.get("extra") or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Advanced JSON is not valid JSON: {e}") from e
    if not isinstance(spec, dict):
        raise ValueError("Advanced JSON must be an object")
    for key in ("set", "who", "q", "rawq", "need", "id", "cards", "notes"):
        v = str(form.get(key, "")).strip()
        if v:
            spec[key] = v
        else:
            spec.pop(key, None)
    if not spec.get("who") or not spec.get("set"):
        raise ValueError("Card name and set are required")
    for key in ("need", "id"):
        if spec.get(key):
            try:
                re.compile(spec[key])
            except re.error as e:
                raise ValueError(f"'{key}' is not a valid regular expression: {e}") from e
    if isinstance(spec.get("pop"), dict) and spec["pop"].get("row"):
        try:
            re.compile(spec["pop"]["row"])
        except re.error as e:
            raise ValueError(f"pop.row is not a valid regular expression: {e}") from e
    for key, prefix in (("max", "max_"), ("rawmax", "rawmax_")):
        vals = {}
        for b in ("1", "2", "3", "any"):
            raw = str(form.get(prefix + b, "")).strip().lstrip("$").replace(",", "")
            if raw:
                try:
                    vals[b] = float(raw)
                except ValueError as e:
                    raise ValueError(f"Ceiling '{raw}' is not a number") from e
                if not math.isfinite(vals[b]) or vals[b] < 0:
                    raise ValueError(f"Ceiling '{raw}' must be a number of zero or more")
        if vals:
            spec[key] = vals
        else:
            spec.pop(key, None)
    refs = [r.strip() for r in str(form.get("refs", "")).split(",") if r.strip()]
    if refs:
        spec["refs"] = refs
    else:
        spec.pop("refs", None)
    every, keep = form.get("art_all") or [], set(form.get("keep") or [])
    # Exclusions for art not on the form (its file is missing just now) are kept as they were.
    prev = set(form.get("prev_skip") or []) | set_of(spec.get("skip"))
    skip = [a for a in sorted(prev) if a not in every]
    skip += [a for a in every if a not in keep]
    if skip:
        spec["skip"] = skip
    else:
        spec.pop("skip", None)
    if spec.get("set") not in match.UA_SETS and not spec.get("q"):
        raise ValueError("Cards outside Union Arena need an eBay search (PSA 10 query)")
    return spec
