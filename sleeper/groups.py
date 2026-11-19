"""How the listings page groups its cards: by deal rating, series, rarity or card."""

from __future__ import annotations

import re
from typing import Any, Callable

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


def rarity(w: dict) -> str:
    """The rarity or print treatment the listings of a want share, for grouping the listings page."""
    m = UA_RARITY.search(str(w.get("cards") or ""))
    if m:
        return m.group(1) + m.group(2)
    who = str(w.get("who") or "").lower()
    return next((label for key, label in MTG_TREATMENTS if key in who), "Other")


# How the listings page can group its cards: the label on the chip, and each row's group title.
GROUPS: dict[str, tuple[str, Callable[[dict], str]]] = {
    "rating": ("Deal rating", lambda r: RATING_LABELS.get(r["v"].rating, r["v"].rating.title())),
    "set": ("Series", lambda r: r["want"].get("set") or "No set"),
    "rarity": ("Rarity", lambda r: rarity(r["want"])),
    "card": ("Card", lambda r: r["want"].get("who") or "Unnamed card"),
    "none": ("None", lambda r: ""),
}


def grouped(rows: list[dict], by: str, sort_key: Callable[[dict], Any]) -> list[tuple[str, list]]:
    """Rows split into titled groups, keeping their order inside each group.

    Deal-rating groups run great to high; the others start with the group holding the best row
    under the current sort, so the top of the page is still the top of the sort.
    """
    title = GROUPS[by][1]
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(title(r), []).append(r)
    if by == "rating":
        order = {v: i for i, v in enumerate(RATING_LABELS.values())}
        return sorted(out.items(), key=lambda g: order.get(g[0], len(order)))
    return sorted(out.items(), key=lambda g: sort_key(g[1][0]))
