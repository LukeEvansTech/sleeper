"""Which eBay listings count for a want, and what it would pay for them.

A want is a dict (stored as JSON in the wants table). Keys, all optional except set and who:

    set       short tag for the game or set: FMA, EVA, CSM, AOT (Union Arena), DBS, FIN, TLA, ...
    who       display name; for Union Arena wants also the words a title must contain
    match     words that must all appear in the title, overriding who
    q / rawq  free-form eBay query for the PSA search / the raw search (anything not Union Arena)
    id        regex the title must match (card number or name)
    need      another regex the title must match
    max       PSA ceilings in USD by star bucket: {"1": 80, "2": 800, "3": ..., "any": ...}
    rawmax    raw ceilings by star bucket; raw_max a flat raw ceiling
    raw       false to skip the raw search
    nostar    raw listings with no star marker still count
    needstar  PSA listings must carry a star marker
    refs      official-art filenames for the photo check
    cards     free-text card numbers ("011, 012 (1★, 2★)") used to find Union Arena refs
    pop       {"term": ..., "row": regex} picks this card's PSA pop rows
    lang      "japanese" or "chinese": a print in that language, so titles and sellers naming it
              count, and titles naming another language (English included) don't
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse

FOREIGN = re.compile(
    r"japan|\bjp\b|\bjpn\b|japanese|chinese|\bcn\b|\bcsv ?\d|korean|\bfrench\b|german|italian|"
    r"spanish|espa[nñ]ol|"
    r"portugu[eê]s|\bthai\b|indonesian|ua\d\dbt|uapr|"
    r"ua37|ua44|ex\d\dbt|\bsao\b|proxy|custom|orica|fan ?art|playmat|sleeve",
    re.I,
)
FOREIGN_SELLER = re.compile(
    r"(Located in|from) (Japan|Hong Kong|China|Korea|Taiwan|Germany|France|Italy|"
    r"Spain|Belgium|Netherlands|Australia)"
)
# Japanese Union Arena codes: promos, boosters, starter decks (UA44ST) and expansions (EX10BT).
JP_CODES = r"\buapr\b|\bua\d\d(?:bt|st)?\b|\bex\d\dbt\b"
# Language markers in a title. English set codes: UE07BT, UEX07BT, UE07ST, UEPR.
LANGS = {
    "english": re.compile(r"english|\beng?\b|\b[u]ex?\d\d(?:bt|st)\b|\b[u]epr\b", re.I),
    "japanese": re.compile(r"japan(?:ese)?|\bjpn?\b|" + JP_CODES, re.I),
    "chinese": re.compile(r"chinese|simplified|\bcn\b|\bcsv?\d|\bcsv \d", re.I),
}
# Where sellers of each language's cards are, as match.FOREIGN_SELLER names them.
LANG_PLACES = {"japanese": ("Japan",), "chinese": ("China", "Hong Kong", "Taiwan")}
# Graders other than PSA. ACE, TAG and ARS are also words, so they count only before a grade
# ("TAG 9", "TAG GEM MINT 10"), never before a card number ("Ace 10-131S").
OTHER_GRADER = re.compile(
    r"\b(bgs|beckett|cgc|sgc|hga|[a]gs)\b"
    r"|\b(tag|ace|ars)(?:\s+(?:grad\w*|gem|mint|pristine))*\s*\d{1,2}(?:\.5)?\b(?![-/.]\w)",
    re.I,
)
GRADED = re.compile(r"\b(psa|bgs|cgc|tag|ace|beckett|sgc|ars)\b", re.I)

# Union Arena sets the character wants cover: eBay OR-words for the PSA search, the set name for raw
# searches, and the title hint a listing must carry to count.
UA_SETS = {
    "FMA": {
        "words": '(fma,fullmetal,alchemist,ue09bt,"union arena")',
        "raw": "fullmetal alchemist",
        "hint": r"union arena|ue09|uepr|fma-1",
    },
    "EVA": {
        "words": '(evangelion,ue15bt,"union arena")',
        "raw": "evangelion",
        "hint": r"union arena|ue15|uepr|eva-1",
    },
    "CSM": {
        "words": '("chainsaw man",csm,ue22bt,"union arena")',
        "raw": "chainsaw man",
        "hint": r"union arena|ue22|uepr|csm-1",
    },
    "AOT": {
        "words": '("attack on titan",aot,ue10bt,uex07bt,"union arena")',
        "raw": "attack on titan",
        "hint": r"union arena|ue10|uex07|uepr|aot-[12]",
    },
}
SET_HINT = {k: re.compile(v["hint"], re.I) for k, v in UA_SETS.items()}
JP_UA_HINT = re.compile(JP_CODES, re.I)
# A Union Arena print in the card list of a want, e.g. UE22BT/CSM-1-056_p1 (set, card id, parallel).
PRINT_ID = re.compile(r"\b([A-Z0-9]+)/([A-Z]+-(?:\d-)?(?:\d{3}|AP\d{2})(?:_p\d)?)")
# The card number a seller writes in a title, e.g. EVA-1-083 or CSM-1-AP03.
TITLE_NO = re.compile(r"\b[A-Z]{2,4}-\d-(\d{3}|AP\d{2})(?:_p\d)?(?![0-9A-Za-z])", re.I)


def card_numbers(cards: str) -> set[str]:
    """Card numbers a want lists, from print ids and bare numbers ("081, 082", "UEPR 010/012")."""
    out = {i.split("-")[-1].split("_")[0].upper() for _, i in PRINT_ID.findall(cards or "")}
    rest = PRINT_ID.sub(" ", cards or "")
    card_id = re.compile(r"\b[A-Z]{2,4}-\d-(\d{3}|AP\d{2})(?:_p\d)?(?![0-9])", re.I)
    out |= {n.upper() for n in card_id.findall(rest)}  # EVA-1-081(_p1) without its set prefix
    rest = card_id.sub(" ", rest)
    out |= {n.upper() for n in re.findall(r"(?<![A-Za-z0-9_])(AP\d{2})(?!\d)", rest, re.I)}
    out |= set(re.findall(r"(?<!\d)(\d{3})(?!\d)", rest))  # card ids are gone; 081-082 is two
    return out


# A card number written on its own in a title: "#023", "(023)", " 023 ". Only zero-led numbers, so a
# grade, a pop count or a "/250" serial is never read as one.
BARE_NO = re.compile(r"(?<![\w/.-])#?(0\d{2})(?![\w/])(?!\s*/)")  # "#001 /250" is a serial


def print_of(w: dict) -> tuple[str, str | None] | None:
    """(card number, star bucket) when the whole card list is one print, e.g. ("012", "2") for
    "UE09BT/FMA-1-012_p1 SR★★". The bucket is "-" for a rarity with no stars (a promo's "UR") and
    None when no rarity is written, so the star count is unknown and not checked."""
    cards = w.get("cards") or ""
    found = PRINT_ID.findall(cards)
    if len(found) != 1:
        return None
    if len(re.findall(r"\b[A-Z]{2,4}-\d-(?:\d{3}|AP\d{2})", cards)) != 1:
        return None  # "…012_p1 SR★★, FMA-1-012_p2 SR★" is two prints of one number
    num = found[0][1].split("-")[-1].split("_")[0].upper()
    if card_numbers(cards) != {num}:  # "…082_p1 SR★, 081" lists another card too
        return None
    m = re.search(PRINT_ID.pattern + r"\s+([A-Z]{1,3})(★*)(?=[\s,]|$)", cards)
    if not m:
        return num, None
    return num, str(len(m.group(4))) if m.group(4) else "-"


def comp_id(w: dict) -> int:
    """The want id sold comps are kept under: a split print shares its family's comps."""
    return int(w.get("family") or w["_id"])


US_ZIP = "97035"  # the Oregon forwarding address: eBay prices shipping and eligibility for it


def stars(title: str) -> str:
    """Star bucket from a listing title: 3 (serial), 2 (signature), 1 (alt art), - (none)."""
    t = re.sub(r"(\d)\s*-\s*star", r"\1 star", title.lower())
    t = re.sub(r"\b([123])\s*[★☆]", r"\1 star", t)
    if re.search(r"3 ?stars?|★★★|☆☆☆|sr\*\*\*|serial", t):
        return "3"
    if re.search(r"2 ?stars?|★★|☆☆|sr\*\*|signature", t):
        return "2"
    # "*" only next to a rarity ("SR*", "R*"): sellers also use it as a separator ("Lust * SR * NM").
    if re.search(r"1 ?star|alt(ernate)? ?art|parallel|★|☆|\b(?:ur|sr|r|u|c)\*", t):
        return "1"
    return "-"


def stated_stars(title: str) -> str:
    """The star count a title states outright ("2 star", "★★", "SR**"), else "-". Unlike stars(),
    words like "parallel" or "alt art" say nothing about the tier, so they don't count here."""
    t = re.sub(r"(\d)\s*-\s*star", r"\1 star", title.lower())
    t = re.sub(r"\b([123])\s*[★☆]", r"\1 star", t)
    for n in ("3", "2", "1"):
        k = int(n)
        if re.search(
            rf"\b{n} ?stars?\b|(?<![★☆]){'[★☆]' * k}(?![★☆])|\b(?:ur|sr|r|u|c)\*{{{k}}}(?!\*)", t
        ):
            return n
    return "-"


def searches(w: dict) -> list[tuple[str, str]]:
    """(kind, query) pairs for one want; kind is psa or raw."""
    if "q" in w:
        out = [("psa", w["q"])]
        if w.get("rawq") and w.get("raw", True):
            out.append(("raw", w["rawq"]))
        return out
    if w.get("set") not in UA_SETS:
        return []
    name = w.get("match") or w["who"]
    ua = UA_SETS[w["set"]]
    words = ua["words"]
    if w.get("lang") == "japanese":  # Japanese listings carry UAPR, not the English set codes
        words = re.sub(r",[u]ex?\d\dbt", "", words)[:-1] + ",uapr,japanese)"
    out = [("psa", f"{name} psa 10 {words}")]
    if w.get("raw", True):
        out.append(("raw", f"union arena {ua['raw']} {name} -psa -bgs -cgc -tag"))
    return out


def search_url(q: str, sold: bool = False) -> str:
    p = {"_nkw": q, "_ipg": "240", "_sop": "15", "_stpos": US_ZIP}
    if sold:
        p |= {"LH_Sold": "1", "LH_Complete": "1", "_sop": "13"}
    return "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode(p)


def keep(w: dict, kind: str, title: str, text: str) -> bool:
    """Does this listing (title plus the search card's full text) belong to the want?"""
    t = title.lower()
    # A free-form query (q) drops the name check, but explicit match words still apply.
    words = (w.get("match") or ("" if "q" in w else w["who"])).lower().split()
    if any(x not in t for x in words):
        return False
    for key in ("id", "need"):
        if w.get(key) and not re.search(w[key], t, re.I):
            return False
    if w.get("set") in SET_HINT and not SET_HINT[w["set"]].search(t):
        if not (w.get("lang") == "japanese" and JP_UA_HINT.search(t)):
            return False
    if w.get("set") in UA_SETS and w.get("cards"):
        # A title naming a card number the want doesn't list is another card (083, not 081/082).
        seen = {n.upper() for n in TITLE_NO.findall(title)}
        listed = card_numbers(w["cards"])
        if seen and listed and not seen & listed:  # no readable numbers: no check
            return False
        one = print_of(w)
        if one:  # a single print: a bare number or a star count in the title must agree too
            bare = set(BARE_NO.findall(title)) if not seen else set()
            if bare and one[0] not in bare:
                return False
            if one[1] is not None and stated_stars(title) not in ("-", one[1]):
                return False
    t_foreign, text_foreign = t, text
    lang = w.get("lang")
    if lang in LANG_PLACES:  # a Japanese or Chinese print: its own language is not foreign
        if any(rx.search(t) for name, rx in LANGS.items() if name != lang):
            return False  # names another language: another print
        t_foreign = LANGS[lang].sub(" ", t)
        for place in LANG_PLACES[lang]:
            text_foreign = text_foreign.replace(place, "")
    if FOREIGN.search(t_foreign) or FOREIGN_SELLER.search(text_foreign):
        return False
    if w.get("set") == "DBS" and re.search(r"signed|autograph|\bauto\b|sabat|schemmel|nadolny", t):
        return False  # voice-actor autographs, not the printed Masters signature SPR
    if kind == "psa":
        if w.get("needstar") and stars(title) == "-":
            return False
        if OTHER_GRADER.search(t):
            return False  # PSA only: a CGC, BGS or "TAG 10, PSA 10 equivalent" slab
        return bool(re.search(r"psa ?10|psa gem", t))
    return not GRADED.search(t) and (bool(w.get("nostar")) or stars(title) != "-")


def money(s: str) -> float:
    m = re.search(r"[\d,]+\.?\d*", s or "")
    return float(m.group(0).replace(",", "")) if m else 0.0


def shipping(text: str) -> float | None:
    """Delivery cost from a search card's text; 0 for free delivery, None when not shown."""
    if re.search(r"free (delivery|shipping)", text, re.I):
        return 0.0
    m = re.search(r"\+\s*\$([\d,]+\.?\d*)\s*(delivery|shipping)", text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def ceiling(w: dict, kind: str, star: str, defaults: dict) -> float | None:
    mx = w.get("max", {})
    if kind == "raw":
        rawmax = w.get("rawmax", {})
        if star in rawmax:
            return float(rawmax[star])
        if "any" in rawmax:
            return float(rawmax["any"])
        if "raw_max" in w:
            return float(w["raw_max"])
        return float(defaults["raw_1star_max"]) if star == "1" else None
    if star in mx:
        return float(mx[star])
    if "any" in mx:
        return float(mx["any"])
    return float(defaults["psa_1star_max"]) if star == "1" else None


LEFT = re.compile(r"\b((?:\d+[dhm] )*\d+[dhm]) left\b")


def left_of(text: str) -> tuple[str, dt.timedelta | None]:
    """An auction's countdown as shown ("1d 4h left", "35m left") and as a duration."""
    m = LEFT.search(text)
    if not m:
        return "", None
    n = {u: int(v) for v, u in re.findall(r"(\d+)([dhm])", m.group(1))}
    return m.group(0), dt.timedelta(days=n.get("d", 0), hours=n.get("h", 0), minutes=n.get("m", 0))
