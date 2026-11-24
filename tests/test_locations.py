"""Location parsing: strings from boards and from the user become places, with no network.

Purpose: the region filter and the location evaluator both need to
read free-text locations such as "San Francisco, CA | Seattle, WA" and "Remote - US". They
share this one offline parser so they can never disagree about where a role is.
Logic: each test feeds strings through parse_location, parse_locations, in_regions or
place_matches and checks the Place values. A string that cannot be parsed is expected to give
no place at all, never a guess.
Guardrails: strings here are fictional postings text. No test opens a socket; the network
import allowlist in tests/test_network_imports.py covers src/jobsearch/locations.py.
QA checklist: every documented behavior has a named test; a new alias needs
one case here.
"""

from __future__ import annotations

import time

import pytest

from jobsearch import locations
from jobsearch.locations import (
    Place, in_regions, parse_location, parse_locations, place_label, place_matches, title_places,
)


def test_pipe_separated_us_cities_give_two_places_with_regions():
    places = parse_location("San Francisco, CA | Seattle, WA")
    assert places == [
        Place("US", "CA", "San Francisco", False),
        Place("US", "WA", "Seattle", False),
    ]


def test_remote_us_is_a_remote_place_in_the_us():
    assert parse_location("Remote - US") == [Place("US", None, None, True)]


def test_hybrid_nyc_resolves_the_hint_to_new_york():
    assert parse_location("Hybrid (NYC)") == [Place("US", "NY", "New York", False)]


def test_berlin_germany_gives_germany():
    assert parse_location("Berlin, Germany") == [Place("DE", None, "Berlin", False)]


def test_multiple_locations_gives_no_place():
    assert parse_location("Multiple locations") == []


def test_a_place_that_is_not_in_the_table_is_never_guessed():
    assert parse_location("Atlantis") == []
    assert parse_location("Springfield") == []
    assert parse_location("") == []
    assert parse_location("   ") == []


def test_austin_in_two_spellings_is_one_place_in_input_order():
    places = parse_locations(["Austin, TX", "Austin, Texas"])
    assert places == [Place("US", "TX", "Austin", False)]


def test_parse_locations_flattens_and_keeps_first_seen_order():
    places = parse_locations(["Seattle, WA | Boston, MA", "Boston, Massachusetts", "Berlin"])
    assert [p.city for p in places] == ["Seattle", "Boston", "Berlin"]


@pytest.mark.parametrize("text", [
    "Austin, TX; Denver, CO", "Austin, TX / Denver, CO", "Austin, TX or Denver, CO",
    "Austin, TX | Denver, CO", "Austin, TX OR Denver, CO",
])
def test_separators_pipe_semicolon_slash_and_or_all_split(text):
    assert [p.city for p in parse_location(text)] == ["Austin", "Denver"]


def test_bare_remote_is_a_remote_place_with_no_country():
    assert parse_location("Remote") == [Place(None, None, None, True)]


@pytest.mark.parametrize("text", ["Remote, United States", "US-Remote", "Remote (US)", "Remote US",
                                  "United States (Remote)"])
def test_remote_forms_for_the_us(text):
    assert parse_location(text) == [Place("US", None, None, True)]


def test_remote_in_a_list_of_countries_keeps_each_country():
    places = parse_location("Remote (US or Canada)")
    assert places == [Place("US", None, None, True), Place("CA", None, None, True)]


@pytest.mark.parametrize("text, countries", [
    ("US, Canada", ["US", "CA"]),
    ("Canada, US", ["CA", "US"]),
    ("United States, Canada", ["US", "CA"]),
    ("Germany, France", ["DE", "FR"]),
    ("Remote - United States, Canada", ["US", "CA"]),
])
def test_a_comma_list_of_countries_gives_each_country(text, countries):
    places = parse_location(text)
    assert [p.country for p in places] == countries
    assert all(p.city is None and p.region is None for p in places)


def test_a_comma_list_of_countries_passes_the_region_filter_in_either_order():
    assert in_regions(parse_locations(["US, Canada"]), ["US"]) is True
    assert in_regions(parse_locations(["Canada, US"]), ["US"]) is True
    assert in_regions(parse_locations(["Canada, US"]), ["MX"]) is False


def test_city_state_and_city_country_commas_still_parse_as_one_place():
    assert parse_location("New York, NY") == [Place("US", "NY", "New York", False)]
    assert parse_location("Remote, US") == [Place("US", None, None, True)]
    assert parse_location("Toronto, ON, Canada") == [Place("CA", "ON", "Toronto", False)]


def test_bay_area_and_other_metros_resolve():
    assert parse_location("San Francisco Bay Area") == [Place("US", "CA", "San Francisco", False)]
    assert parse_location("Washington DC") == [Place("US", "DC", "Washington", False)]
    assert parse_location("Washington, D.C.") == [Place("US", "DC", "Washington", False)]
    assert parse_location("New York City") == [Place("US", "NY", "New York", False)]
    assert parse_location("Chicago") == [Place("US", "IL", "Chicago", False)]


def test_state_and_country_forms():
    assert parse_location("Texas") == [Place("US", "TX", None, False)]
    assert parse_location("TX") == [Place("US", "TX", None, False)]
    assert parse_location("United States") == [Place("US", None, None, False)]
    assert parse_location("USA") == [Place("US", None, None, False)]
    assert parse_location("Austin, Texas, United States") == [Place("US", "TX", "Austin", False)]
    assert parse_location("New York, New York, United States") == [
        Place("US", "NY", "New York", False)]


def test_canadian_provinces_and_cities():
    assert parse_location("Toronto, ON") == [Place("CA", "ON", "Toronto", False)]
    assert parse_location("Vancouver, British Columbia, Canada") == [
        Place("CA", "BC", "Vancouver", False)]


def test_uk_forms_all_resolve_to_gb():
    assert parse_location("London, UK") == [Place("GB", None, "London", False)]
    assert parse_location("London, England, United Kingdom") == [Place("GB", None, "London", False)]
    assert parse_location("London") == [Place("GB", None, "London", False)]


def test_a_two_letter_code_that_is_both_a_state_and_a_country_is_ambiguous_alone():
    assert parse_location("CA") == []
    assert parse_location("Remote - CA") == [Place(None, None, None, True)]


def test_a_city_in_the_table_decides_an_ambiguous_trailing_code():
    assert parse_location("Berlin, DE") == [Place("DE", None, "Berlin", False)]
    assert parse_location("Wilmington, DE") == [Place("US", "DE", "Wilmington", False)]


def test_accents_and_case_do_not_matter():
    assert parse_location("SÃO PAULO, brazil") == [Place("BR", None, "São Paulo", False)]
    assert parse_location("zurich, switzerland") == [Place("CH", None, "Zurich", False)]


def test_unrecognized_text_next_to_a_known_piece_does_not_hide_the_known_piece():
    places = parse_location("Austin, TX | Multiple locations")
    assert places == [Place("US", "TX", "Austin", False)]


def test_in_regions_country_codes_and_eu():
    germany = parse_locations(["Berlin, Germany"])
    america = parse_locations(["Seattle, WA"])
    assert in_regions(germany, ["EU"]) is True
    assert in_regions(america, ["EU"]) is False
    assert in_regions(america, ["US"]) is True
    assert in_regions(germany, ["US"]) is False
    assert in_regions(germany, ["us", "de"]) is True


def test_uk_and_switzerland_are_not_in_the_eu_but_match_their_own_codes():
    london = parse_locations(["London, UK"])
    zurich = parse_locations(["Zurich"])
    assert in_regions(london, ["EU"]) is False
    assert in_regions(london, ["GB"]) is True
    assert in_regions(london, ["UK"]) is True
    assert in_regions(zurich, ["EU"]) is False


def test_in_regions_empty_places_is_unknown():
    assert in_regions([], ["EU"]) is None
    assert in_regions([], ["US"]) is None


def test_in_regions_one_matching_place_is_enough():
    places = parse_locations(["London, UK | New York, NY"])
    assert in_regions(places, ["US"]) is True


def test_in_regions_is_unknown_when_a_remote_place_has_no_country():
    places = parse_locations(["Remote", "London, UK"])
    assert in_regions(places, ["US"]) is None
    assert in_regions(places, ["GB"]) is True


def test_in_regions_with_nothing_allowed_is_unknown():
    assert in_regions(parse_locations(["Berlin"]), []) is None


def test_place_matches_when_a_posting_city_equals_a_user_city():
    posting = parse_locations(["Austin, TX | Denver, CO"])
    assert place_matches(posting, parse_locations(["austin, texas"])) is True
    assert place_matches(posting, parse_locations(["Boston, MA"])) is False


def test_place_matches_a_region_when_the_user_gave_only_a_region():
    posting = parse_locations(["Austin, TX"])
    assert place_matches(posting, parse_locations(["Texas"])) is True
    assert place_matches(posting, parse_locations(["Colorado"])) is False


def test_place_matches_a_country_when_the_user_gave_only_a_country():
    posting = parse_locations(["Berlin"])
    assert place_matches(posting, parse_locations(["Germany"])) is True
    assert place_matches(posting, parse_locations(["France"])) is False


def test_place_matches_is_false_when_either_list_is_empty():
    anywhere = parse_locations(["Austin, TX"])
    assert place_matches([], anywhere) is False
    assert place_matches(anywhere, []) is False
    assert place_matches([], []) is False


def test_place_matches_does_not_match_the_same_city_name_in_another_country():
    assert place_matches(parse_locations(["Paris, TX"]), parse_locations(["Paris, France"])) is False


def test_a_remote_only_user_place_matches_nothing_geographic():
    assert place_matches(parse_locations(["Austin, TX"]), [Place(None, None, None, True)]) is False


# Larger tables, zones, title places and the US parse misses ----------------------

@pytest.mark.parametrize("text, code", [
    ("Japan", "JP"), ("China", "CN"), ("South Korea", "KR"), ("Korea", "KR"), ("Taiwan", "TW"),
    ("Hong Kong", "HK"), ("Philippines", "PH"), ("Vietnam", "VN"), ("Thailand", "TH"),
    ("Indonesia", "ID"), ("Malaysia", "MY"), ("Pakistan", "PK"), ("Bangladesh", "BD"),
    ("Sri Lanka", "LK"), ("United Arab Emirates", "AE"), ("UAE", "AE"), ("Saudi Arabia", "SA"),
    ("Qatar", "QA"), ("Turkey", "TR"), ("Türkiye", "TR"), ("Egypt", "EG"),
    ("South Africa", "ZA"), ("Nigeria", "NG"), ("Kenya", "KE"), ("Morocco", "MA"),
    ("Argentina", "AR"), ("Chile", "CL"), ("Colombia", "CO"), ("Peru", "PE"),
    ("Uruguay", "UY"), ("Costa Rica", "CR"), ("Ukraine", "UA"), ("Serbia", "RS"),
    ("Iceland", "IS"),
])
def test_each_new_country_name_gives_its_code(text, code):
    assert parse_location(text) == [Place(code, None, None, False)]


@pytest.mark.parametrize("text, code", [
    ("Tokyo", "JP"), ("Osaka", "JP"), ("Shanghai", "CN"), ("Beijing", "CN"),
    ("Shenzhen", "CN"), ("Seoul", "KR"), ("Taipei", "TW"), ("Manila", "PH"),
    ("Bangkok", "TH"), ("Jakarta", "ID"), ("Kuala Lumpur", "MY"), ("Ho Chi Minh City", "VN"),
    ("Dubai", "AE"), ("Abu Dhabi", "AE"), ("Riyadh", "SA"), ("Istanbul", "TR"),
    ("Cairo", "EG"), ("Lagos", "NG"), ("Nairobi", "KE"), ("Johannesburg", "ZA"),
    ("Cape Town", "ZA"), ("Buenos Aires", "AR"), ("Santiago", "CL"), ("Bogota", "CO"),
    ("Bogotá", "CO"), ("Medellin", "CO"), ("Lima", "PE"), ("Montevideo", "UY"),
    ("Kyiv", "UA"),
])
def test_each_new_city_resolves_to_its_country(text, code):
    places = parse_location(text)
    assert [p.country for p in places] == [code]
    assert places[0].city is not None


def test_a_city_with_its_country_keeps_the_city():
    assert parse_location("Shanghai, China") == [Place("CN", None, "Shanghai", False)]


def test_a_new_city_never_beats_a_us_state_code_next_to_it():
    assert parse_location("Lima, OH") == [Place("US", "OH", "Lima", False)]


@pytest.mark.parametrize("text", ["CO", "AR", "ID", "MA", "PE"])
def test_codes_shared_by_a_state_or_province_and_a_country_are_ambiguous_alone(text):
    assert parse_location(text) == []


def test_a_city_that_names_its_country_decides_a_shared_code():
    assert parse_location("Medellin, CO") == [Place("CO", None, "Medellin", False)]
    assert parse_location("Denver, CO") == [Place("US", "CO", "Denver", False)]


def test_a_bullet_or_middle_dot_separates_places_like_a_pipe():
    expected = [Place("US", "NY", "New York", False), Place("US", None, None, False)]
    assert parse_location("New York, NY \u2022 United States") == expected
    assert parse_location("New York, NY \u00b7 United States") == expected


def test_remote_in_the_us_is_a_remote_us_place():
    assert parse_location("Remote in the US") == [Place("US", None, None, True)]
    assert parse_location("Remote in the United States") == [Place("US", None, None, True)]
    assert parse_location("based in Canada") == [Place("CA", None, None, False)]


def test_a_city_list_ending_in_remote_in_the_us_passes_the_us_filter():
    places = parse_locations(["San Francisco, Seattle, New York, Chicago, Remote in the US"])
    assert in_regions(places, ["US"]) is True


@pytest.mark.parametrize("text", ["In-Office", "In Office", "Hybrid", "On-site", "Onsite"])
def test_work_arrangement_words_alone_carry_no_place(text):
    assert parse_location(text) == []


@pytest.mark.parametrize("text, zone", [
    ("APAC", "APAC"), ("apac", "APAC"), ("Asia Pacific", "APAC"), ("Asia-Pacific", "APAC"),
    ("APJ", "APJ"), ("APJC", "APJC"), ("EMEA", "EMEA"), ("Europe", "Europe"),
    ("LATAM", "LATAM"), ("Latin America", "LATAM"), ("South America", "South America"),
    ("North America", "North America"), ("AMER", "AMER"), ("Americas", "AMER"),
    ("Middle East", "Middle East"), ("MENA", "MENA"), ("Africa", "Africa"), ("ANZ", "ANZ"),
    ("DACH", "DACH"), ("Nordics", "Nordics"), ("Benelux", "Benelux"),
])
def test_a_region_token_parses_to_a_zone_with_no_country(text, zone):
    assert parse_location(text) == [Place(None, None, None, False, zone)]


def test_remote_in_a_zone_keeps_the_remote_flag_and_the_zone():
    assert parse_location("Remote - APAC") == [Place(None, None, None, True, "APAC")]
    assert parse_location("Remote in EMEA") == [Place(None, None, None, True, "EMEA")]


def test_every_zone_member_is_a_country_the_module_knows():
    for zone, members in locations._ZONE_MEMBERS.items():
        assert members, zone
        assert members <= set(locations._COUNTRY_NAMES), zone
    assert locations.EU_COUNTRIES <= locations._ZONE_MEMBERS["EMEA"]


def test_a_zone_holding_none_of_the_allowed_countries_is_outside():
    assert in_regions(parse_locations(["APAC"]), ["US"]) is False
    assert in_regions(parse_locations(["LATAM"]), ["DE", "EU"]) is False
    assert in_regions(parse_locations(["Japan", "APAC"]), ["US"]) is False


def test_a_zone_holding_an_allowed_country_stays_unknown():
    assert in_regions(parse_locations(["APAC"]), ["SG"]) is None
    assert in_regions(parse_locations(["EMEA"]), ["EU"]) is None
    assert in_regions(parse_locations(["North America"]), ["US"]) is None
    assert in_regions(parse_locations(["Europe"]), ["UK"]) is None


def test_a_country_inside_the_allowed_regions_beats_a_zone():
    assert in_regions(parse_locations(["Austin, TX | APAC"]), ["US"]) is True


def test_a_zone_next_to_a_bare_remote_is_still_unknown_for_a_user_who_could_work_there():
    assert in_regions(parse_locations(["Remote", "APAC"]), ["US"]) is None


# title_places ---------------------------------------------------------------------------------

@pytest.mark.parametrize("title, expected", [
    ("Revenue Operations Manager, Japan", [Place("JP", None, None, False)]),
    ("Principal HR - APJC Benefits and Program Manager",
     [Place(None, None, None, False, "APJC")]),
    ("Workplace Operations Manager - Singapore", [Place("SG", None, "Singapore", False)]),
    ("Program Manager (APAC)", [Place(None, None, None, False, "APAC")]),
    ("Account Executive, US", [Place("US", None, None, False)]),
    ("Sales Lead, UK and Ireland", [Place("GB", None, None, False), Place("IE", None, None, False)]),
    ("Manager, Tokyo", [Place("JP", None, "Tokyo", False)]),
    ("Manager, T\u00fcrkiye", [Place("TR", None, None, False)]),
])
def test_title_places_reads_the_place_a_title_names(title, expected):
    assert title_places(title) == expected


def test_title_places_uses_up_a_span_so_north_america_is_never_also_america():
    assert title_places("Director, North America") == [
        Place(None, None, None, False, "North America")]
    assert title_places("Director, Northern Ireland") == [Place("GB", None, None, False)]


@pytest.mark.parametrize("title", [
    "IT Program Manager", "Manager, ID Operations", "Operations Manager, CA", "Us and Them Lead",
    "Manager, Washington", "Program Manager, Texas", "Director, Ontario", "Robotics Engineer",
    "us program manager", "", "Manager, in",
])
def test_title_places_never_reads_a_two_letter_code_a_state_or_a_lowercase_us(title):
    assert title_places(title) == []


@pytest.mark.parametrize("title", [
    "Territory Manager, New Mexico", "Sales Rep - Lima, Ohio", "Account Exec Paris, Texas",
    "Manager Cairo, IL", "Site Lead, Dublin, CA", "Rep, Lima, North Carolina",
    "Manager, Cairo, Illinois", "Lead, New Brunswick", "Manager, Vancouver, Washington",
    "Lead, Toronto, CA", "Engineer London ON SITE",
])
def test_title_places_leaves_a_us_or_canadian_place_unread(title):
    """A name that is a US state or province, or a city followed by one, reads as no
    place, so the region verdict stays unknown."""
    assert title_places(title) == []


@pytest.mark.parametrize("title, expected", [
    ("Account Manager - Toronto, ON", [Place("CA", "ON", "Toronto", False)]),
    ("Sales Lead, Vancouver, BC", [Place("CA", "BC", "Vancouver", False)]),
    ("Lead, Toronto, Ontario", [Place("CA", "ON", "Toronto", False)]),
    ("Engineer - London, ON", [Place("CA", "ON", "London", False)]),
    ("Manager, Paris, Ontario", [Place("CA", "ON", "Paris", False)]),
    ("Analyst - London, UK", [Place("GB", None, "London", False), Place("GB", None, None, False)]),
    ("Ops Lead - Sydney, NSW", [Place("AU", None, "Sydney", False)]),
])
def test_title_places_reads_a_province_after_a_place_as_canada(title, expected):
    """Only a US state after a place makes it doubtful. A Canadian province
    after it names Canada, and a region the module does not know changes nothing."""
    assert title_places(title) == expected


@pytest.mark.parametrize("title, expected", [
    ("Sales Lead, Lima, Peru", [Place("PE", None, "Lima", False), Place("PE", None, None, False)]),
    ("Account Executive, Paris", [Place("FR", None, "Paris", False)]),
    ("Manager, New Zealand", [Place("NZ", None, None, False)]),
])
def test_title_places_still_reads_a_genuinely_foreign_place(title, expected):
    assert title_places(title) == expected


def test_title_places_lists_each_place_once_in_title_order():
    places = title_places("Japan Lead, Tokyo and Japan, then APAC")
    assert [place_label(p) for p in places] == ["Japan", "Tokyo", "APAC"]


def test_title_places_is_bounded_and_ignores_non_text():
    start = time.perf_counter()
    assert title_places("Manager " * 20000 + "Japan") == []  # past the 300 character cap
    assert title_places("Manager, " + "x" * 100000) == []
    assert time.perf_counter() - start < 2.0
    assert title_places(None) == []  # type: ignore[arg-type]


def test_place_label_names_a_city_a_zone_or_a_country():
    assert place_label(Place("JP", None, "Tokyo", False)) == "Tokyo"
    assert place_label(Place(None, None, None, False, "APAC")) == "APAC"
    assert place_label(Place("JP", None, None, False)) == "Japan"
    assert place_label(Place("US", None, None, False)) == "United States"
    assert place_label(Place(None, None, None, True)) == "an unstated place"


def test_a_huge_hostile_string_is_handled_quickly_and_bounded():
    text = "a, " * 50000 + " | ".join(["x"] * 50000)
    start = time.perf_counter()
    result = parse_location(text)
    assert time.perf_counter() - start < 2.0
    assert isinstance(result, list)


# A foreign city followed by a code its own country also uses --------------

def test_a_us_state_that_disagrees_with_a_known_foreign_city_reads_as_no_place():
    """Lucid's "Amsterdam, NH": NH is a US state and also Noord-Holland, a region of the
    Netherlands, so the place is unknown and the title decides."""
    assert parse_location("Amsterdam, NH") == []
    assert parse_locations(["Amsterdam, NH"]) == []
    assert in_regions(parse_locations(["Amsterdam, NH"]), ["US"]) is None


@pytest.mark.parametrize("text, expected", [
    ("Dublin, OH", Place("US", "OH", "Dublin", False)),
    ("Paris, TX", Place("US", "TX", "Paris", False)),
    ("Dublin, CA", Place("US", "CA", "Dublin", False)),
])
def test_a_real_us_town_sharing_a_foreign_citys_name_stays_us(text, expected):
    """OH is not a region of Ireland, TX is not a region of France: the US reading is kept."""
    assert parse_location(text) == [expected]


def test_the_shared_region_code_table_is_well_formed():
    table = locations._SHARED_REGION_CODES
    foreign = {row[1] for row in locations._CITY_ROWS} - {"US"}
    assert table and set(table) <= foreign
    for country, codes in table.items():
        assert codes and all(len(c) == 2 and c in locations._US_CODES for c in codes), country
    assert "NH" in table["NL"] and "OH" not in table["IE"] and "TX" not in table["FR"]
    assert table["IE"] == frozenset({"CO", "KY", "MN", "MO"})
    assert sum(len(codes) for codes in table.values()) == 81


@pytest.mark.parametrize("text, expected", [
    ("Amsterdam, Netherlands", [Place("NL", None, "Amsterdam", False)]),
    ("Amsterdam, NL", [Place("NL", None, "Amsterdam", False)]),
    ("Paris, France", [Place("FR", None, "Paris", False)]),
    ("Boston, MA", [Place("US", "MA", "Boston", False)]),
    ("Springfield, IL", [Place("US", "IL", "Springfield", False)]),
    ("London, ON", [Place("CA", "ON", "London", False)]),
    ("Medellin, CO", [Place("CO", None, "Medellin", False)]),
])
def test_a_known_city_with_its_own_country_or_state_reads_as_before(text, expected):
    assert parse_location(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("San Francisco, CA | Seattle, WA", [Place("US", "CA", "San Francisco", False),
                                         Place("US", "WA", "Seattle", False)]),
    ("Lima, OH", [Place("US", "OH", "Lima", False)]),
    ("Denver, CO", [Place("US", "CO", "Denver", False)]),
    ("Hybrid (NYC)", [Place("US", "NY", "New York", False)]),
    ("New York, NY • United States", [Place("US", "NY", "New York", False),
                                           Place("US", None, None, False)]),
    ("Remote - US", [Place("US", None, None, True)]),
])
def test_the_us_parse_fixes_from_02_16_still_hold(text, expected):
    assert parse_location(text) == expected


# comma-joined lists: every place is read, in input order -----------------------

@pytest.mark.parametrize("text, expected", [
    ("New York, NY, London, UK",
     [Place("US", "NY", "New York", False), Place("GB", None, "London", False)]),
    ("London, UK, New York, NY",
     [Place("GB", None, "London", False), Place("US", "NY", "New York", False)]),
    ("Boston, MA, New York, NY",
     [Place("US", "MA", "Boston", False), Place("US", "NY", "New York", False)]),
])
def test_a_comma_joined_list_gives_every_place_in_input_order(text, expected):
    assert parse_location(text) == expected


def test_a_comma_joined_list_with_a_us_city_is_in_the_us():
    assert in_regions(parse_location("New York, NY, London, UK"), ["US"]) is True


@pytest.mark.parametrize("text", [
    "Berlin, Germany", "Toronto, ON, Canada", "San Francisco, CA, United States"])
def test_a_city_with_its_own_region_and_country_is_still_one_place(text):
    assert len(parse_location(text)) == 1


def test_a_long_comma_string_is_read_without_error_and_stays_bounded():
    places = parse_location("x, NY, " * 285)
    assert len(places) <= locations._MAX_PIECES


def test_a_street_address_before_a_city_does_not_hide_the_city():
    assert parse_location("Floor 3, 100 Main St, Boston, MA") == [
        Place("US", "MA", "Boston", False)]


def test_a_non_us_city_and_country_reads_as_a_usable_place():
    assert parse_location("Berlin, Germany") == [Place("DE", None, "Berlin", False)]


def test_read_places_reports_the_part_nobody_could_pair():
    places, unread = locations.read_places(["Phoenix, Seattle, WA", "Austin, TX"])
    assert places == [Place("US", "WA", "Seattle", False), Place("US", "TX", "Austin", False)]
    assert unread == ["Phoenix"]
    assert locations.read_places(["Springfield"]) == ([], ["Springfield"])


def test_user_place_reads_a_location_or_a_region_code():
    assert locations.user_place("Phoenix, AZ")[1] == []
    assert locations.user_place("CA") == ([Place("CA", None, None, False)], [])
    assert locations.user_place("Phoenix") == ([], ["Phoenix"])
