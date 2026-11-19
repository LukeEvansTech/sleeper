"""A wants checklist to share: a self-contained HTML file per set, with the card pictures inlined.

It answers "which cards am I looking for?" for someone sorting bulk: picture, name and print code
of every want that is switched on. Prices and ceilings stay out of it.
"""

from __future__ import annotations

import base64
import datetime as dt
import re
from pathlib import Path
from typing import Callable

import cv2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from . import photos
from .db import DB
from .groups import ordered_sets, rarity

EXPORT_LAST = "export_last"  # kv: the print sets, treatments and format last exported
# A Magic want names its print in its title, e.g. "Summon: Anima FIN #364" or "Yojimbo SLD #7004".
MTG_CODE = re.compile(r"(?<![\w'])([A-Za-z]{3})\s*#\s*\w+")
UA_CODE = re.compile(r"^\s*([A-Z0-9]+)/")
# What a print code means to someone holding the card: the set, and where its cards come from.
SET_NAMES = {
    "FIN": "Final Fantasy, main set (play and collector boosters)",
    "FIC": "Final Fantasy Commander (Commander decks and collector boosters only)",
    "FCA": "Final Fantasy Through the Ages (one in some play boosters)",
    "SLD": "Secret Lair (sold direct, never in boosters)",
    "TLA": "Avatar: The Last Airbender, main set",
    "TLE": "Avatar: The Last Airbender Eternal",
    "SPM": "Marvel's Spider-Man, main set",
    "LTR": "The Lord of the Rings, main set",
    "LTC": "The Lord of the Rings Commander",
}
UA_SET = re.compile(r"^U([AE])(\d+)(BT|ST)$")  # Union Arena: Japanese (A) or English (E) prints
NUMBER = re.compile(r"#\s*0*(\d+)|-(\d+)(?:_|\s|$)")
THUMB_W = 240  # px: sharp enough to recognise the art, small enough for a few hundred cards


def thumb(path: Path) -> str:
    """The picture as a small JPEG data URI, or "" when it can't be read."""
    img = cv2.imread(str(path))
    if img is None:
        return ""
    h, w = img.shape[:2]
    if w > THUMB_W:
        img = cv2.resize(img, (THUMB_W, round(h * THUMB_W / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 72])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode() if ok else ""


def print_set(w: dict) -> str:
    """The print set the card of a want comes from: FIN, FIC, SLD, UE15BT, UEPR... else its series."""
    m = UA_CODE.match(str(w.get("cards") or ""))
    if m:
        return m.group(1).upper()
    series = w.get("set") or ""
    for m in MTG_CODE.finditer(str(w.get("who") or "")):
        code = m.group(1)  # a set code is written in capitals, or is the series itself ("fin #526")
        if code.isupper() or code.upper() == series.upper():
            return code.upper()
    return series


def set_name(code: str) -> str:
    """A plain description of a print code, or "" when it is not one we know."""
    if code in SET_NAMES:
        return SET_NAMES[code]
    m = UA_SET.match(code)
    if m:
        lang = "Japanese" if m.group(1) == "A" else "English"
        kind = "booster" if m.group(3) == "BT" else "starter deck"
        return f"Union Arena {lang} {kind} {int(m.group(2))}"
    if code in ("UEPR", "UAPR"):
        return f"Union Arena {'English' if code == 'UEPR' else 'Japanese'} promo"
    return ""


def number(w: dict) -> int:
    """The collector number of the card a want is after, for sorting; very large when it has none."""
    for text in (str(w.get("who") or ""), str(w.get("cards") or "")):
        m = NUMBER.search(text)
        if m:
            return int(m.group(1) or m.group(2))
    return 10**9


def pick_id(w: dict) -> str:
    """The checkbox value for the print set of a want: series and code, as promos share UEPR."""
    return f"{w.get('set') or ''}:{print_set(w)}"


def chosen(w: dict, codes: set[str], kinds: set[str]) -> bool:
    """Is a want on and inside the picked print sets and treatments (an empty pick takes all)?"""
    return bool(w.get("_enabled")) and (
        (not codes or pick_id(w) in codes) and (not kinds or rarity(w) in kinds)
    )


def checklist(
    wants: list[dict], order: list[str], refs_dir: Path, skipped: Callable[[dict], set[str]]
) -> list[dict]:
    """Wants that are on, one group per print set (series in the saved order, then code), each
    card with its first kept picture, sorted by collector number."""
    by_code: dict[tuple[str, str], list[dict]] = {}
    for w in wants:
        if not w.get("_enabled"):
            continue
        by_code.setdefault((w.get("set") or "", print_set(w)), []).append(w)
    home = refs_dir.resolve()  # a ref naming a path outside the refs folder is never read
    groups = []
    for series in ordered_sets({x for x, _ in by_code}, order):
        # the series' own main set first, then the rest by code
        codes = sorted((c != series, c) for x, c in by_code if x == series)
        for _, code in codes:
            cards = []
            for w in sorted(by_code[(series, code)], key=lambda w: (number(w), w.get("who", ""))):
                refs = [r for r in photos.refs_for(w, refs_dir) if r.resolve().is_relative_to(home)]
                kept = [r for r in refs if r.name not in skipped(w)]  # all unticked: no picture
                cards.append(
                    {
                        "who": w.get("who", ""),
                        "cards": w.get("cards", ""),
                        "img": thumb(kept[0]) if kept else "",
                    }
                )
            title = code if code == series or not series else f"{series}: {code}"
            groups.append({"title": title or "No set", "about": set_name(code), "cards": cards})
    return groups


def pdf(html: str) -> bytes:
    """The checklist printed to A4 by the headless browser the sweeps already use."""
    from playwright.sync_api import (  # pylint: disable=import-outside-toplevel
        sync_playwright,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chromium", args=["--disable-dev-shm-usage"])
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            margin = {"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"}
            return page.pdf(format="A4", print_background=True, margin=margin)
        finally:
            browser.close()


def add_routes(
    app: FastAPI,
    db: DB,
    refs_dir: Path,
    tpl: Jinja2Templates,
    set_order: str,
    skipped: Callable[[dict], set[str]],
) -> None:
    """/export (pick print sets, treatments and format) and /export/download (the file)."""

    @app.get("/export", response_class=HTMLResponse)
    def export_page(request: Request):
        wants = [w for w in db.wants() if w.get("_enabled")]
        codes: dict[str, dict[str, int]] = {}  # series -> print set -> cards
        kinds: dict[str, int] = {}
        for w in wants:
            series = codes.setdefault(w.get("set") or "", {})
            series[pick_id(w)] = series.get(pick_id(w), 0) + 1
            kinds[rarity(w)] = kinds.get(rarity(w), 0) + 1
        last = db.get(EXPORT_LAST, {})
        return tpl.TemplateResponse(
            request,
            "export.html",
            {
                "series": [
                    (x, sorted(codes[x].items()))
                    for x in ordered_sets(codes, db.get(set_order, []))
                ],
                "kinds": sorted(kinds.items()),
                "set_name": set_name,
                "total": len(wants),
                "codes_on": set(last.get("codes") or []),
                "kinds_on": set(last.get("kinds") or []),
                "fmt": last.get("format", "html"),
            },
        )

    @app.get("/export/download")
    def export_download(request: Request):
        q = request.query_params
        codes, kinds = set(q.getlist("code")), set(q.getlist("kind"))
        fmt = "pdf" if q.get("format") == "pdf" else "html"
        wants = [w for w in db.wants() if chosen(w, codes, kinds)]
        if not wants:
            raise HTTPException(404, "no cards match those print sets and treatments")
        db.put(EXPORT_LAST, {"codes": sorted(codes), "kinds": sorted(kinds), "format": fmt})
        groups = checklist(wants, db.get(set_order, []), refs_dir, skipped)
        today = dt.date.today().isoformat()
        picked = sorted({c.split(":", 1)[-1] or "no-code" for c in codes})
        label = "-".join(picked) if 0 < len(picked) <= 3 else "cards"
        name = re.sub(r"[^A-Za-z0-9-]+", "-", label).strip("-") or "cards"
        html = tpl.get_template("export_file.html").render(
            groups=groups, today=today, only=", ".join(picked) if 0 < len(picked) <= 3 else ""
        )
        filename = f"card-wants-{name}-{today}.{fmt}"
        disposition = {"content-disposition": f'attachment; filename="{filename}"'}
        if fmt == "pdf":
            return Response(pdf(html), media_type="application/pdf", headers=disposition)
        return HTMLResponse(html, headers=disposition)
