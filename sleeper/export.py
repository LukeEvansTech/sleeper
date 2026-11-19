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
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import photos
from .db import DB
from .groups import ordered_sets

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


def checklist(
    wants: list[dict], order: list[str], refs_dir: Path, skipped: Callable[[dict], set[str]]
) -> list[tuple[str, list[dict]]]:
    """Wants that are on, by set in the saved order, each with its first kept picture."""
    by_set: dict[str, list[dict]] = {}
    for w in wants:
        if not w.get("_enabled"):
            continue
        refs = photos.refs_for(w, refs_dir)
        kept = [r for r in refs if r.name not in skipped(w)]  # all unticked: no picture
        by_set.setdefault(w.get("set") or "", []).append(
            {
                "who": w.get("who", ""),
                "cards": w.get("cards", ""),
                "img": thumb(kept[0]) if kept else "",
            }
        )
    return [(x, by_set[x]) for x in ordered_sets(by_set, order)]


def add_routes(
    app: FastAPI,
    db: DB,
    refs_dir: Path,
    tpl: Jinja2Templates,
    set_order: str,
    skipped: Callable[[dict], set[str]],
) -> None:
    """/export (pick a set) and /export/download (the file)."""

    @app.get("/export", response_class=HTMLResponse)
    def export_page(request: Request):
        wants = [w for w in db.wants() if w.get("_enabled")]
        counts: dict[str, int] = {}
        for w in wants:
            counts[w.get("set") or ""] = counts.get(w.get("set") or "", 0) + 1
        sets = ordered_sets(counts, db.get(set_order, []))
        return tpl.TemplateResponse(
            request, "export.html", {"sets": [(x, counts[x]) for x in sets], "total": len(wants)}
        )

    @app.get("/export/download", response_class=HTMLResponse)
    def export_download(
        request: Request, set: str | None = None  # pylint: disable=redefined-builtin
    ):
        wants = db.wants()
        if set is not None:  # ?set= (empty) is the wants with no set, not every set
            wants = [w for w in wants if (w.get("set") or "") == set]
            if not wants:
                raise HTTPException(404, "no wants in that set")
        groups = checklist(wants, db.get(set_order, []), refs_dir, skipped)
        today = dt.date.today().isoformat()
        label = "all-sets" if set is None else set or "no-set"
        name = re.sub(r"[^A-Za-z0-9-]+", "-", label).strip("-") or "set"
        resp = tpl.TemplateResponse(
            request,
            "export_file.html",
            {"groups": groups, "today": today, "only": "" if set is None else set or "No set"},
        )
        resp.headers["content-disposition"] = (
            f'attachment; filename="card-wants-{name}-{today}.html"'
        )
        return resp
