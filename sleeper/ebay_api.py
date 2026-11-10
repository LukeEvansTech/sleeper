"""eBay's Browse API as a drop-in for the headless browser in ebay.py.

It takes the same search URLs and returns the same row shape (id, title, price, text, img), so the
sweep and the title filters in match.py work unchanged. The ``text`` field is rebuilt from the API's
structured fields in the wording the search cards use ("+$4.50 delivery", "3 bids", "1d 4h left",
"or Best Offer", "Located in Japan"), which is what match.shipping and match.keep read.

Two gaps against the website: the Browse API has no sold-listings search (sold comps still arrive
through POST /api/sold), and its ``q`` has no ``-word`` exclusion, so excluded words are applied to
titles here instead.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

log = logging.getLogger(__name__)

API = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"
PAGE = 200  # the Browse API's maximum page size
MAX_ROWS = 400
MAX_PAGES = 10
Q_MAX = 100  # longer q strings are truncated by eBay
# ISO codes for the seller countries match.FOREIGN_SELLER rejects, in the words a search card uses.
COUNTRIES = {
    "JP": "Japan",
    "HK": "Hong Kong",
    "CN": "China",
    "KR": "Korea",
    "TW": "Taiwan",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "BE": "Belgium",
    "NL": "Netherlands",
    "AU": "Australia",
    "GB": "United Kingdom",
}

Fetch = Callable[[urllib.request.Request], tuple[int, bytes]]


def _fetch(req: urllib.request.Request) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - fixed https host
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def split_query(q: str) -> tuple[str, list[str]]:
    """eBay website query -> (Browse API q, excluded words)."""
    keep, drop = [], []
    # A quoted phrase or a parenthesised (a,b) group is one term.
    for word in re.findall(r'-?"[^"]*"|-?\([^)]*\)|\S+', q):
        if word.startswith("-") and len(word) > 1:
            body = word[1:]
            parts = body[1:-1].split(",") if body.startswith("(") else [body]
            drop += [x.strip().strip('"').lower() for x in parts if x.strip().strip('"')]
        else:
            keep.append(word)
    return " ".join(keep).strip(), drop


def _money(amount: dict | None) -> float | None:
    try:
        return float(amount["value"]) if amount else None
    except (KeyError, TypeError, ValueError):
        return None


def time_left(end: str, now: dt.datetime | None = None) -> str:
    """'2d 4h left' / '3h 12m left' from an ISO end time, in the search card's wording."""
    try:
        at = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((at - (now or dt.datetime.now(dt.timezone.utc))).total_seconds())
    if secs <= 0:
        return ""
    d, rest = divmod(secs, 86400)
    h, m = rest // 3600, rest % 3600 // 60
    return f"{d}d {h}h left" if d else f"{h}h {m}m left"


def row(item: dict, now: dt.datetime | None = None) -> dict | None:
    """One itemSummary as a search-card row; None when it has no usable id or price."""
    item_id = item.get("legacyItemId") or (item.get("itemId", "").split("|") + ["", ""])[1]
    title = item.get("title", "")
    options = item.get("buyingOptions") or []
    usd = _money(item.get("currentBidPrice")) if "AUCTION" in options else None
    priced = item.get("currentBidPrice") if usd is not None else item.get("price")
    if usd is None:
        usd = _money(item.get("price"))
    if not item_id or not title or usd is None:
        return None
    bits = [title, f"${usd:,.2f}"]
    ship: dict = next(iter(item.get("shippingOptions") or []), {})
    cost = _money(ship.get("shippingCost"))
    if cost == 0:
        bits.append("Free delivery")
    elif cost is not None:
        bits.append(f"+${cost:,.2f} delivery")
    if "AUCTION" in options:
        n = int(item.get("bidCount") or 0)
        bits.append(f"{n} bid" if n == 1 else f"{n} bids")
        left = time_left(item.get("itemEndDate", ""), now)
        if left:
            bits.append(left)
    if "BEST_OFFER" in options:
        bits.append("or Best Offer")
    country = (item.get("itemLocation") or {}).get("country", "")
    if country:
        bits.append(f"Located in {COUNTRIES.get(country, country)}")
    img = (item.get("image") or {}).get("imageUrl") or next(
        (i.get("imageUrl", "") for i in item.get("thumbnailImages") or []), ""
    )
    out = {
        "id": str(item_id),
        "title": title,
        "price": f"${usd:,.2f}",
        "text": " ".join(bits),
        "img": img,
        # The listing's own currency: eBay.com shows a Canadian auction in dollars converted from CAD.
        "cur": (priced or {}).get("convertedFromCurrency") or (priced or {}).get("currency", ""),
    }
    if "AUCTION" in options:
        try:  # the exact end, in the sweep's local clock like its other timestamps
            end = dt.datetime.fromisoformat(item.get("itemEndDate", "").replace("Z", "+00:00"))
            out["ends"] = end.astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
        except ValueError:
            pass
    return out


class BrowseAPI:
    """Search and item photos through the Browse API, with the Browser's interface."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tries: int = 4,
        pause: float = 3.0,
        fetch: Fetch = _fetch,
    ):
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self.tries, self.pause, self._fetch = tries, pause, fetch
        self._token, self._expires = "", 0.0
        self._warned_sold = False

    def __enter__(self) -> "BrowseAPI":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def _auth(self) -> str:
        if not self._token or time.time() > self._expires:
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE})
            req = urllib.request.Request(
                f"{API}/identity/v1/oauth2/token",
                data=body.encode(),
                headers={
                    "Authorization": f"Basic {self._basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            status, raw = self._fetch(req)
            if status != 200:
                raise RuntimeError(f"eBay token request failed: HTTP {status}")
            got = json.loads(raw)
            self._token = got["access_token"]
            self._expires = time.time() + int(got.get("expires_in", 7200)) - 120
        return self._token

    def _get(self, path: str, params: dict[str, str], zip_code: str = "") -> dict | None:
        """GET with retries; None when every try failed."""
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(self.tries):
            try:
                headers = {
                    "Authorization": f"Bearer {self._auth()}",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                    "Accept": "application/json",
                }
                if zip_code:
                    ctx = urllib.parse.quote(f"country=US,zip={zip_code}")
                    headers["X-EBAY-C-ENDUSERCTX"] = f"contextualLocation={ctx}"
                status, raw = self._fetch(urllib.request.Request(url, headers=headers))
                if status == 200:
                    return json.loads(raw)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.debug("browse api attempt %d failed: %s", attempt, type(e).__name__)
                status = 0
            if status == 401:
                self._token = ""  # expired or revoked: fetch a new one on the next try
            elif 400 <= status < 500 and status != 429:
                log.warning("browse api %s: HTTP %d", path, status)
                return None
            time.sleep(self.pause * (attempt + 1))
        return None

    def search(self, url: str) -> list[dict] | None:
        """Rows for an eBay website search URL; [] for no results, None if every try failed."""
        p = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if p.get("LH_Sold") == "1":
            if not self._warned_sold:
                log.info("the Browse API has no sold search; sold comps come from /api/sold")
                self._warned_sold = True
            return []
        q, drop = split_query(p.get("_nkw", ""))
        if not q:  # nothing to search for is a failed search, not an empty result
            log.warning("no search terms left in %r", p.get("_nkw", ""))
            return None
        if len(q) > Q_MAX:  # eBay would cut it, dropping terms; fail rather than search wrongly
            log.warning("query over %d characters for the Browse API: %s", Q_MAX, q)
            return None
        excluded = (
            re.compile(r"\b(" + "|".join(map(re.escape, drop)) + r")\b", re.I) if drop else None
        )
        out: list[dict] = []
        # The row cap counts kept rows, as on the website where eBay applied exclusions itself.
        for offset in range(0, MAX_PAGES * PAGE, PAGE):
            params = {
                "q": q,
                "limit": str(PAGE),
                "offset": str(offset),
                "sort": "price",
                # Without buyingOptions the API returns fixed-price listings only.
                "filter": "deliveryCountry:US,buyingOptions:{FIXED_PRICE|AUCTION}",
            }
            got = self._get("/buy/browse/v1/item_summary/search", params, p.get("_stpos", ""))
            if got is None:  # a partial result would retire the listings on the missing page
                return None
            items = got.get("itemSummaries") or []
            out += [
                r for r in map(row, items) if r and not (excluded and excluded.search(r["title"]))
            ]
            if len(out) >= MAX_ROWS or offset + PAGE >= int(got.get("total") or 0) or not items:
                break
        else:  # page limit hit with results left: incomplete, so leave tracked listings alone
            return None
        return out[:MAX_ROWS]

    def item_photos(self, item_id: str) -> list[str]:
        """Full-size photo URLs for a listing, gallery order."""
        got = self._get(
            "/buy/browse/v1/item/get_item_by_legacy_id", {"legacy_item_id": str(item_id)}
        )
        if not got:
            return []
        imgs: list[Any] = [got.get("image") or {}] + list(got.get("additionalImages") or [])
        urls = [re.sub(r"s-l\d+", "s-l1600", i.get("imageUrl", "")) for i in imgs]
        return list(dict.fromkeys(u for u in urls if u))

    def item(self, item_id: str) -> dict | None:
        """The listing as eBay shows it now, ended or not; None when every try failed."""
        return self._get(
            "/buy/browse/v1/item/get_item_by_legacy_id", {"legacy_item_id": str(item_id)}
        )
