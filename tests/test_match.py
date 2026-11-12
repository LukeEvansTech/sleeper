from sleeper import match

EDWARD = {"set": "FMA", "who": "Edward Elric", "max": {"2": 1000}, "rawmax": {"2": 300}}
DEFAULTS = {"psa_1star_max": 80, "raw_1star_max": 20}


def test_stars_buckets():
    assert match.stars("Union Arena FMA Edward 3 Star Serial") == "3"
    assert match.stars("Edward SR** signature UE09BT") == "2"
    assert match.stars("Edward alt art FMA-1-011") == "1"
    assert match.stars("Edward Elric UE09BT common") == "-"


def test_bare_asterisk_is_a_separator_not_a_star():
    # listing 820176195712: a plain SR, the asterisks are punctuation
    assert match.stars("Lust * UE09BT/FMA-1-090 SR * Union Arena * Fullmetal Alchemist * NM") == "-"
    assert match.stars("*Sealed* Mikasa Ackermann (Winner) Attack on Titan Union Arena") == "-"
    assert match.stars("Union Arena | Father (SR*) | UE09BT/FMA-1-079 | NM") == "1"
    assert match.stars("Angel Devil (010) (R*) - UE22BT: CHAINSAW MAN") == "1"
    assert match.stars("Attack On Titan Vol. 2 / Mikasa Ackerman (066) / U*") == "1"


def test_union_arena_searches_use_set_words():
    (k1, q1), (k2, q2) = match.searches(EDWARD)
    assert (k1, k2) == ("psa", "raw")
    assert "psa 10" in q1 and "ue09bt" in q1
    assert q2.startswith("union arena fullmetal alchemist Edward Elric")


def test_free_form_want_without_rawq_has_psa_search_only():
    w = {"set": "DBS", "who": "Vegito", "q": "vegito bt7-118 psa 10"}
    assert match.searches(w) == [("psa", "vegito bt7-118 psa 10")]


def test_keep_needs_union_arena_marker_and_grade():
    t = "PSA 10 Edward Elric 2 Star Union Arena FMA"
    assert match.keep(EDWARD, "psa", t, t)
    assert not match.keep(EDWARD, "psa", "PSA 10 Edward Elric 2 Star", "")
    assert not match.keep(EDWARD, "psa", "Edward Elric 2 Star Union Arena", "")


def test_keep_drops_japanese_and_foreign_sellers():
    t = "PSA 10 Edward Elric Japanese Union Arena"
    assert not match.keep(EDWARD, "psa", t, t)
    t2 = "PSA 10 Edward Elric Union Arena 2 star"
    assert not match.keep(EDWARD, "psa", t2, t2 + " Located in Japan")


def test_japanese_want_takes_japanese_titles_and_sellers():
    w = {"set": "PKM", "who": "Hoopa", "q": "hoopa psa 10", "id": "hoopa", "lang": "japanese"}
    t = "PSA 10 Hoopa 155/XY-P Movie Promo 2015 Pokemon Card Japanese"
    assert match.keep(w, "psa", t, t + " Located in Japan")
    assert not match.keep({**w, "lang": None}, "psa", t, t)
    assert not match.keep(w, "psa", "PSA 10 Hoopa Japanese proxy", "")  # still not a fake
    assert not match.keep(w, "psa", "PSA 10 Hoopa Korean", "")  # nor another language
    assert not match.keep(w, "psa", t, t + " Located in China")
    denji = {"set": "CSM", "who": "Denji", "cards": "UA16BT/CSM-1-001", "lang": "japanese"}
    t3 = "PSA 10 Union Arena Chainsaw Man Denji UA16BT/CSM-1-001 Japanese"
    assert match.keep(denji, "psa", t3, t3)  # a Japanese set code is not foreign
    for code in ("UE22BT/CSM-1-001 English", "UEX07BT/CSM-1-001", "UEPR/CSM-1-001"):
        t4 = f"PSA 10 Union Arena Chainsaw Man Denji {code}"
        assert not match.keep(denji, "psa", t4, t4 + " Located in Japan"), code  # English prints


def test_japanese_union_arena_promo_needs_no_english_set_hint():
    rei = {"set": "EVA", "who": "Rei", "match": "Rei Ayanami", "cards": "UAPR/EVA-1-071 R"}
    t = "PSA 10 Rei Ayanami Evangelion UAPR Japanese"
    assert match.keep({**rei, "lang": "japanese"}, "psa", t, t + " Located in Japan")
    assert not match.keep(rei, "psa", t, t)
    for t in (
        "PSA 10 Rei Ayanami Union Arena UA44ST Japanese",
        "PSA 10 Rei Ayanami EX10BT Japanese",
    ):
        assert match.keep({**rei, "lang": "japanese"}, "psa", t, t), t


def test_chinese_want_takes_chinese_titles_and_sellers():
    w = {"set": "PKM", "who": "Sylveon", "q": "sylveon psa 10", "id": "sylveon", "lang": "chinese"}
    t = "PSA 10 2026 Pokemon TCG Sylveon ex CSV9.5C-234/208 SAR Exclusive Chinese"
    assert match.keep(w, "psa", t, t + " Located in Hong Kong")
    assert not match.keep(w, "psa", "PSA 10 Sylveon ex SAR Japanese", "")
    assert not match.keep(w, "psa", t, t + " Located in Japan")
    assert not match.keep(w, "psa", "PSA 10 Sylveon ex Spanish", " Located in Hong Kong")


def test_cn_titles_and_uk_sellers():
    w = {"set": "PKM", "who": "Sylveon", "q": "sylveon psa 10", "id": "sylveon", "lang": "chinese"}
    t = "The Pokémon Company Sylveon ex 234/208 CSV 9.5 CS CN Holo SAR 270HP PSA 10"
    assert match.keep(w, "psa", t, t + " Located in United Kingdom")  # a UK seller, no import VAT
    edward = "PSA 10 Edward Elric 2 Star Union Arena UE07BT"
    assert match.keep(EDWARD, "psa", edward, edward + " Located in United Kingdom")
    for cn in ("PSA 10 Edward Elric 2 Star Union Arena CN", "PSA 10 Edward Elric CSV 9.5 SAR"):
        assert not match.keep(EDWARD, "psa", cn, cn), cn  # a Chinese print, for an English want


def test_psa_wants_take_psa_slabs_only():
    for t in (
        "TAG 10 Edward Elric 2 Star Union Arena (PSA 10 equivalent)",
        "BGS 9.5 Edward Elric 2 Star Union Arena PSA 10 candidate",
        "CGC 10 Pristine Edward Elric 2 Star Union Arena PSA 10",
        "Edward Elric 2 Star Union Arena ACE 10 not PSA 10",
        "TAG 9 Edward Elric 2 Star Union Arena PSA 10 candidate",
        "TAG GEM MINT 10 Edward Elric 2 Star Union Arena PSA 10 equivalent",
    ):
        assert not match.keep(EDWARD, "psa", t, t), t
    t = "PSA 10 Edward Elric 2 Star Union Arena"
    assert match.keep(EDWARD, "psa", t, t)
    ace = {"set": "FIN", "who": "Ace", "q": "ace psa 10", "id": "ace"}
    assert match.keep(ace, "psa", "PSA 10 Ace Final Fantasy", "")  # a card named Ace
    assert match.keep(ace, "psa", "Final Fantasy Ace Gem Mint PSA 10", "")
    assert match.keep(ace, "psa", "Final Fantasy Ace Graded PSA 10", "")
    assert match.keep(ace, "psa", "Final Fantasy Ace 10-131S PSA 10", "")  # a card number


def test_japanese_wants_search_japanese_titles():
    rei = {"set": "EVA", "who": "Rei", "match": "Rei Ayanami", "lang": "japanese", "raw": False}
    assert "uapr" in match.searches(rei)[0][1]
    assert "uapr" not in match.searches({**rei, "lang": None})[0][1]
    eren = {"set": "AOT", "who": "x", "match": "Eren Yeager & Mikasa Ackerman", "lang": "japanese"}
    q = match.searches(eren)[0][1]
    assert len(q) <= 100 and "uapr" in q and "ue10bt" not in q  # the Browse API's limit


def test_raw_must_be_ungraded_and_starred():
    assert match.keep(EDWARD, "raw", "Union Arena Edward Elric 2 star", "")
    assert not match.keep(EDWARD, "raw", "Union Arena Edward Elric 2 star BGS 9.5", "")
    assert not match.keep(EDWARD, "raw", "Union Arena Edward Elric common", "")


def test_dbs_autographs_are_not_the_printed_signature():
    w = {"set": "DBS", "who": "Vegito", "q": "x"}
    assert not match.keep(w, "psa", "Vegito SPR PSA 10 signed by Sabat", "")
    assert match.keep(w, "psa", "Vegito SPR PSA 10 BT7-118", "")


def test_ceilings():
    assert match.ceiling(EDWARD, "psa", "2", DEFAULTS) == 1000
    assert match.ceiling(EDWARD, "psa", "1", DEFAULTS) == 80
    assert match.ceiling(EDWARD, "psa", "3", DEFAULTS) is None
    assert match.ceiling(EDWARD, "raw", "2", DEFAULTS) == 300
    assert match.ceiling(EDWARD, "raw", "1", DEFAULTS) == 20
    assert match.ceiling({"max": {"any": 50}}, "psa", "-", DEFAULTS) == 50


def test_money_and_shipping():
    assert match.money("$1,234.50") == 1234.5
    assert match.money("") == 0.0
    assert match.shipping("Buy It Now +$5.64 delivery Located in United States") == 5.64
    assert match.shipping("Free delivery") == 0.0
    assert match.shipping("Buy It Now") is None


def test_raw_any_ceiling():
    w = {"rawmax": {"any": 50}}
    assert match.ceiling(w, "raw", "-", DEFAULTS) == 50
    assert match.ceiling(w, "raw", "1", DEFAULTS) == 50


def test_star_ceiling_beats_any():
    assert match.ceiling({"max": {"1": 80, "any": 1000}}, "psa", "1", DEFAULTS) == 80
    assert match.ceiling({"max": {"1": 80, "any": 1000}}, "psa", "2", DEFAULTS) == 1000


def test_raw_false_skips_rawq():
    assert match.searches({"q": "a", "rawq": "b", "raw": False}) == [("psa", "a")]


def test_match_words_apply_with_custom_query():
    w = {"set": "FIN", "who": "Cloud", "q": "cloud psa 10", "match": "cloud strife"}
    assert not match.keep(w, "psa", "PSA 10 Tifa Lockhart", "")
    assert match.keep(w, "psa", "PSA 10 Cloud Strife surge foil", "")


def test_hyphenated_and_triple_asterisk_stars():
    assert match.stars("Edward 2-Star UE09BT") == "2"
    assert match.stars("Edward SR*** serial") == "3"
    assert match.stars("Edward SR***") == "3"


def test_digit_star_symbol():
    assert match.stars("Edward 2★ UE09BT") == "2"
    assert match.stars("Edward 3☆ serial") == "3"


def test_title_naming_an_unlisted_card_number_is_another_card():
    w = {"set": "EVA", "who": "Production Model-02", "cards": "081, 082"}
    assert not match.keep(w, "raw", "Union Arena Production Model-02 UE15BT/EVA-1-083 SR* NM", "")
    assert match.keep(w, "raw", "Union Arena Production Model-02 UE15BT/EVA-1-081 SR* NM", "")
    aki = {"set": "CSM", "who": "Aki Hayakawa", "cards": "UE22BT/CSM-1-056_p1 SR★★"}
    assert not match.keep(
        aki, "raw", "Union Arena Aki Hayakawa Chainsaw Man UE22BT/CSM-1-053 U*", ""
    )
    assert match.card_numbers("011, 012 (1★, 2★), 040, UEPR 010/012/039") == {
        "010",
        "011",
        "012",
        "039",
        "040",
    }


def test_card_numbers_accept_ids_without_a_set_prefix():
    assert match.card_numbers("EVA-1-081") == {"081"}
    assert match.card_numbers("UE15BT/EVA-1-082_p1 SR★, 081") == {"081", "082"}
    w = {"set": "EVA", "who": "Production Model-02", "cards": "EVA-1-081"}
    assert match.keep(w, "raw", "Union Arena Production Model-02 UE15BT/EVA-1-081 SR* NM", "")


def test_card_numbers_read_parallels_and_action_points():
    assert match.card_numbers("EVA-1-081_p1") == {"081"}
    assert match.card_numbers("AP03, AP04") == {"AP03", "AP04"}
    w = {"set": "CSM", "who": "CSM Action Point cards", "q": "x", "cards": "AP03"}
    assert match.keep(w, "psa", "PSA 10 Union Arena Chainsaw Man CSM-1-AP03 Action Point", "")
    odd = {"set": "EVA", "who": "Rei", "cards": "the blue one"}  # nothing readable: no check
    assert match.keep(odd, "raw", "Union Arena Rei Ayanami UE15BT/EVA-1-010 R* NM", "")


def test_title_parallel_suffix_and_hyphenated_lists():
    w = {"set": "EVA", "who": "Production Model-02", "cards": "081-082"}
    assert match.card_numbers("081-082") == {"081", "082"}
    assert not match.keep(w, "raw", "Union Arena Production Model-02 EVA-1-083_p1 SR* NM", "")
    assert match.keep(w, "raw", "Union Arena Production Model-02 EVA-1-082_p1 SR* NM", "")


def test_hyphenated_action_point_lists():
    assert match.card_numbers("AP03-AP04") == {"AP03", "AP04"}


def test_single_print_wants_check_bare_numbers_and_stars():
    w = {
        "set": "FMA",
        "who": "Edward Elric · 012 SR★★",
        "match": "Edward Elric",
        "cards": "UE09BT/FMA-1-012_p1 SR★★",
    }
    assert match.print_of(w) == ("012", "2")
    assert match.print_of({"cards": "UEPR/FMA-1-039_p2 U"}) == ("039", "-")
    assert match.print_of({"cards": "UE09BT/FMA-1-012_p1 SR★★, UE09BT/FMA-1-040_p1 SR★"}) is None

    def keep(t):
        return match.keep(w, "psa", t, t)

    assert keep("Union Arena Edward Elric (012) 2 Star PSA 10")
    assert keep("Union Arena Edward Elric 2 star psa 10 pop 1/250")  # a serial is not a card number
    assert not keep("Union Arena Edward Elric #040 2 star psa 10")
    assert not keep("Union Arena Edward Elric 1 star psa 10")
    assert match.comp_id({"_id": 5, "family": 2}) == 2 and match.comp_id({"_id": 5}) == 5


def test_print_of_needs_one_print_and_reads_unknown_rarity():
    assert match.print_of({"cards": "UE15BT/EVA-1-082_p1 SR★, 081"}) is None  # another card too
    assert match.print_of({"cards": "UE09BT/FMA-1-012_p1 SR★★, FMA-1-012_p2 SR★"}) is None
    assert match.print_of({"cards": "UE22BT/CSM-1-056_p1"}) == ("056", None)
    w = {"set": "CSM", "who": "Aki", "cards": "UE22BT/CSM-1-056_p1"}
    title = "Union Arena Chainsaw Man Aki CSM-1-056 2 Star PSA 10"
    assert match.keep(w, "psa", title, title)  # unknown rarity: any star count passes


def test_stated_stars_ignores_generic_art_words():
    assert match.stated_stars("Edward Elric FMA-1-012_p1 Parallel PSA 10") == "-"
    assert match.stated_stars("Edward SR*** PSA 10") == "3"
    assert match.stated_stars("Edward 1-star alt art") == "1"
    assert match.stated_stars("Edward U* foil") == "1" and match.stated_stars("Edward R** x") == "2"
    w = {
        "set": "FMA",
        "who": "Edward",
        "match": "Edward Elric",
        "cards": "UE09BT/FMA-1-012_p1 SR★★",
    }
    t = "Union Arena Edward Elric FMA-1-012_p1 Parallel PSA 10"
    assert match.keep(w, "psa", t, t)


def test_spaced_serial_is_not_a_card_number():
    assert match.BARE_NO.findall("Union Arena Edward Elric 3 Star Serial #001 /250 PSA 10") == []
    assert match.BARE_NO.findall("Union Arena Lanfan (023) SR*") == ["023"]
