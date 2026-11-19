"""How the listings page groups its cards: by deal rating, series, rarity or card."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

RATING_LABELS = {
    "great": "Great deals",
    "good": "Good deals",
    "unknown": "Not enough sales to rate",
    "fair": "Fair prices",
    "high": "Above the market",
}
# Union Arena rarity in the print list of a want, e.g. "UE15BT/EVA-1-079_p2 SR★★"
UA_RARITY = re.compile(r"\b(UR|SR|R|U|C|AP)(★*)(?=\s*(?:\(|,|$))")
# Magic treatments, most specific first: a want names its print in its title.
MTG_TREATMENTS = (
    ("surge", "Surge foil"),
    ("chocobo track", "Chocobo Track foil"),
    ("neon ink", "Neon Ink foil"),
    ("serialized", "Serialized"),
    ("through the ages", "Through the Ages"),
    ("secret lair", "Secret Lair"),
    ("woodblock", "Woodblock"),
    ("borderless", "Borderless"),
)


def rarity(w: dict, star: str = "") -> str:
    """The rarity or print treatment of a listing, from its want, for grouping the listings page.

    A want listing several Union Arena prints of different rarities is resolved by the listing's
    own star count; when that does not settle it the listing goes under "Mixed".
    """
    found = sorted({a + b for a, b in UA_RARITY.findall(str(w.get("cards") or ""))})
    if len(found) == 1:
        return found[0]
    if found:
        stars = int(star) if str(star).isdigit() else -1
        hits = [x for x in found if x.count("★") == stars]
        return hits[0] if len(hits) == 1 else "Mixed"
    who = str(w.get("who") or "").lower()
    return next((label for key, label in MTG_TREATMENTS if key in who), "Other")


# How the listings page can group its cards: the label on the chip, and each row's group title.
GROUPS: dict[str, tuple[str, Callable[[dict], str]]] = {
    "rating": ("Deal rating", lambda r: RATING_LABELS.get(r["v"].rating, r["v"].rating.title())),
    "set": ("Series", lambda r: r["want"].get("set") or "No set"),
    "rarity": ("Rarity", lambda r: rarity(r["want"], r.get("star", ""))),
    "card": ("Card", lambda r: r["want"].get("who") or "Unnamed card"),
    "none": ("None", lambda r: ""),
}


def ordered_sets(sets: Iterable[str], order: list[str]) -> list[str]:
    """Set names in the saved order; any set the order doesn't name follows, alphabetically."""
    rank = {x: i for i, x in enumerate(order)}
    return sorted(set(sets), key=lambda x: (rank.get(x, len(rank)), x))


def moved(sets: list[str], name: str, step: int) -> list[str]:
    """The set order with one set moved a place earlier (step -1) or later (step 1)."""
    out = list(sets)
    if name not in out:
        return out
    i = out.index(name)
    j = max(0, min(len(out) - 1, i + step))
    out[i], out[j] = out[j], out[i]
    return out


def grouped(
    rows: list[dict],
    by: str,
    sort_key: Callable[[dict], Any],
    set_order: list[str] | None = None,
) -> list[tuple[str, list]]:
    """Rows split into titled groups, keeping their order inside each group.

    Deal-rating groups run great to high and series follow the saved set order; the others start
    with the group holding the best row under the current sort, so the top of the page is still
    the top of the sort.
    """
    title = GROUPS[by][1]
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(title(r), []).append(r)
    if by == "rating":
        order = {v: i for i, v in enumerate(RATING_LABELS.values())}
        return sorted(out.items(), key=lambda g: order.get(g[0], len(order)))
    if by == "set" and set_order is not None:
        names = ordered_sets((r["want"].get("set") or "" for r in rows), set_order)
        rank = {GROUPS["set"][1]({"want": {"set": x}}): i for i, x in enumerate(names)}
        return sorted(out.items(), key=lambda g: rank.get(g[0], len(rank)))
    return sorted(out.items(), key=lambda g: sort_key(g[1][0]))
