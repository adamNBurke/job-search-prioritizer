"""Location strings to places, on the user's machine, with no network call.

Purpose: the region filter and the location evaluator both need to read free-text locations
such as "San Francisco, CA | Seattle, WA", "Remote - US" and "Hybrid (NYC)". They share this one
parser so they can never disagree about where a role is, and no location text ever leaves the
machine (no geocoding service is used, for privacy, cost and terms reasons).
Logic: parse_location splits a string on "|", ";", a bullet or middle dot, " / " and " or "
(outside parentheses), reads "Remote" into the remote flag, drops "Hybrid", "On-site" and
"In-Office", and resolves what is left through a local alias table: US states and DC, Canadian
provinces, a country table (the EU members and the larger economies of Asia, the Middle East,
Africa and the Americas), a list of well-known cities and a zone table (APAC, EMEA and the like,
each a named set of countries). A leading "in", "in the", "within" or "based in" on a token is
dropped. A piece reads from the right: country, then state or province, then the city next to
it. A piece that is a zone name gives a place with a zone and no country. parse_locations
flattens and deduplicates in input order. in_regions answers whether any place sits in a list of
country codes or "EU", with None for unknown: a zone holding none of the allowed countries is
outside, and a zone holding one of them stays unknown rather than guessed. title_places reads
a place named in a posting title, for the one case where the locations name no country and no
zone (the title can reject a role only by naming a place outside the allowed
regions, and a title that names nothing leaves the verdict unknown). It reads zone names, country
names and city names of four or more characters, and the uppercase tokens US, USA and UK, never
any other two-letter code, so "IT Program Manager" is never Italy. place_matches compares a
posting's places with the user's: city first, then region when the user gave only a region, then
country when the user gave only a country.
Guardrails: text that matches no alias gives no place, never a guess. A two-letter code that is
both a US state and a country code (CA, DE, IN, IL, MT and similar) is ambiguous on its own and
gives no place; after a city it is read as a state first, then a country, then a province, unless
the city is in the table and names its country. A US state code after a known city outside the
US reads as no place when the code is also an ISO 3166-2 region code of that city's country
(_SHARED_REGION_CODES, built from the iso-codes data, not from memory): "Amsterdam, NH" is
Noord-Holland as well as New Hampshire, so the title decides the region, while "Dublin, OH", "Dublin, CA" and "Paris, TX" keep reading as US towns. The
residual, accepted: a real US town that shares a known foreign city's name and whose state code
is also a region code of that country (a "Milan, MI", MI being a Milano province code) reads as
no place; its region becomes a gap, never a reject unless the title names a foreign place.
Input is capped at 2000 characters and 25 pieces
so hostile board data cannot stall a scan; a title is capped at 300 characters. Title phrases
come only from this module's own tables, escaped, never from config or posting text. This module
imports no network library; the allowlist in tests/test_network_imports.py enforces it.
QA checklist: a new alias gets one case in tests/test_locations.py; a place that cannot be
resolved stays unknown (in_regions returns None) rather than getting a best guess.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = ["Place", "parse_location", "parse_locations", "read_places", "user_place",
           "in_regions", "place_matches", "title_places", "place_label", "EU_COUNTRIES"]

_MAX_TEXT = 2000
_MAX_PIECES = 25
_MAX_TITLE = 300


@dataclass(frozen=True)
class Place:
    country: str | None
    region: str | None
    city: str | None
    remote: bool
    zone: str | None = None  # a named set of countries ("APAC"); set only when country is None


_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_US_CODES = frozenset(_US_STATES.values())

# For each non-US country of _CITY_ROWS, the two-letter ISO 3166-2 subdivision codes of that
# country that are also US state codes ("Amsterdam, NH": NH is New Hampshire and also
# Noord-Holland, NL-NH). A known foreign city followed by one of its own country's codes reads as
# no place, so the title decides the region. Generated once at build time, never at runtime and
# never from memory, from the Debian iso-codes project's ISO 3166-2 data
# (https://salsa.debian.org/iso-codes-team/iso-codes/-/raw/main/data/iso_3166-2.json, downloaded
# 2026-11-20, top key "3166-2", each entry's "code" being "CC-SUB"): keep the entries whose CC is
# a non-US country of _CITY_ROWS and whose SUB has two characters and is in _US_CODES. That gives
# 81 codes across 19 countries. Regenerate it whenever _CITY_ROWS gains a country; a country with
# no entry here keeps the US reading of a state code, so a missing entry never blanks a place.
_SHARED_REGION_CODES: dict[str, frozenset[str]] = {
    "AE": frozenset({"AZ"}),
    "AU": frozenset({"WA"}),
    "BR": frozenset({"AL", "MA", "MS", "MT", "PA", "SC"}),
    "CH": frozenset({"AR", "NE"}),
    "CL": frozenset({"AR", "CO", "MA"}),
    "CN": frozenset({"HI", "MO", "NM", "SC", "SD"}),
    "CO": frozenset({"DC"}),
    "EG": frozenset({"MN", "MT"}),
    "ES": frozenset({"AL", "AR", "CA", "CO", "CT", "GA", "MA", "MD", "NC", "OR", "RI", "VA"}),
    "FR": frozenset({"NC"}),
    "ID": frozenset({"KS", "LA", "MA", "PA", "RI"}),
    "IE": frozenset({"CO", "KY", "MN", "MO"}),
    "IN": frozenset({"AR", "GA", "LA", "MN", "TN"}),
    "IT": frozenset({
        "AL", "AR", "CA", "CO", "CT", "ME", "MI", "MN", "MO", "MS", "MT", "OR", "PA", "RI", "TN",
        "VA", "VT"}),
    "NG": frozenset({"AK", "DE", "LA", "RI"}),
    "NL": frozenset({"FL", "NH", "UT"}),
    "UY": frozenset({"AR", "CA", "CO", "LA", "MA", "MO", "PA"}),
    "VN": frozenset({"CT"}),
    "ZA": frozenset({"NC"}),
}

_CA_PROVINCES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "nova scotia": "NS", "northwest territories": "NT",
    "nunavut": "NU", "ontario": "ON", "prince edward island": "PE", "quebec": "QC",
    "saskatchewan": "SK", "yukon": "YT",
}
_CA_CODES = frozenset(_CA_PROVINCES.values())

# ISO 3166 alpha-2 code -> names and aliases (the lowercase code itself is always an alias).
_COUNTRY_NAMES: dict[str, tuple[str, ...]] = {
    "AT": ("austria",), "BE": ("belgium",), "BG": ("bulgaria",), "HR": ("croatia",),
    "CY": ("cyprus",), "CZ": ("czechia", "czech republic"), "DK": ("denmark",),
    "EE": ("estonia",), "FI": ("finland",), "FR": ("france",), "DE": ("germany", "deutschland"),
    "GR": ("greece",), "HU": ("hungary",), "IE": ("ireland", "republic of ireland"),
    "IT": ("italy",), "LV": ("latvia",), "LT": ("lithuania",), "LU": ("luxembourg",),
    "MT": ("malta",), "NL": ("netherlands", "the netherlands", "holland"), "PL": ("poland",),
    "PT": ("portugal",), "RO": ("romania",), "SK": ("slovakia",), "SI": ("slovenia",),
    "ES": ("spain",), "SE": ("sweden",),
    "US": ("usa", "united states", "united states of america", "america"),
    "GB": ("uk", "united kingdom", "great britain", "britain", "england", "scotland", "wales",
           "northern ireland"),
    "CA": ("canada",), "CH": ("switzerland",), "NO": ("norway",), "AU": ("australia",),
    "NZ": ("new zealand",), "IN": ("india",), "SG": ("singapore",), "IL": ("israel",),
    "MX": ("mexico",), "BR": ("brazil",),
    "JP": ("japan",), "CN": ("china",), "KR": ("south korea", "korea", "republic of korea"),
    "TW": ("taiwan",), "HK": ("hong kong",), "PH": ("philippines",), "VN": ("vietnam", "viet nam"),
    "TH": ("thailand",), "ID": ("indonesia",), "MY": ("malaysia",), "PK": ("pakistan",),
    "BD": ("bangladesh",), "LK": ("sri lanka",), "AE": ("united arab emirates", "uae"),
    "SA": ("saudi arabia",), "QA": ("qatar",), "TR": ("turkey", "turkiye"), "EG": ("egypt",),
    "ZA": ("south africa",), "NG": ("nigeria",), "KE": ("kenya",), "MA": ("morocco",),
    "AR": ("argentina",), "CL": ("chile",), "CO": ("colombia",), "PE": ("peru",),
    "UY": ("uruguay",), "CR": ("costa rica",), "UA": ("ukraine",), "RS": ("serbia",),
    "IS": ("iceland",),
}
EU_COUNTRIES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
})
_COUNTRY_KEYS: dict[str, str] = {}
for _code, _names in _COUNTRY_NAMES.items():
    _COUNTRY_KEYS[_code.lower()] = _code
    for _name in _names:
        _COUNTRY_KEYS[_name] = _code

# Zones: display name, aliases (the lowercase display name is always an alias), member codes.
# A zone is a named set of countries, not a country: it can put a role outside the user's
# regions (none of its members is allowed) but can never put it inside (it only overlaps).
_ASIA_PACIFIC = frozenset({
    "JP", "CN", "KR", "TW", "HK", "SG", "IN", "AU", "NZ", "PH", "VN", "TH", "ID", "MY", "PK",
    "BD", "LK",
})
_EUROPE = EU_COUNTRIES | {"GB", "CH", "NO", "UA", "RS", "IS"}
_MIDDLE_EAST = frozenset({"IL", "AE", "SA", "QA", "TR", "EG"})
_AFRICA = frozenset({"ZA", "NG", "KE", "MA", "EG"})
_NORTH_AMERICA = frozenset({"US", "CA", "MX"})
_SOUTH_AMERICA = frozenset({"BR", "AR", "CL", "CO", "PE", "UY"})
_LATIN_AMERICA = _SOUTH_AMERICA | {"MX", "CR"}
_ZONE_ROWS: tuple[tuple[str, tuple[str, ...], frozenset[str]], ...] = (
    ("APAC", ("asia pacific", "asia-pacific"), _ASIA_PACIFIC),
    ("APJ", (), _ASIA_PACIFIC),
    ("APJC", (), _ASIA_PACIFIC),
    ("EMEA", (), _EUROPE | _MIDDLE_EAST | _AFRICA),
    ("Europe", (), _EUROPE),
    ("LATAM", ("latin america",), _LATIN_AMERICA),
    ("South America", (), _SOUTH_AMERICA),
    ("North America", (), _NORTH_AMERICA),
    ("AMER", ("americas",), _NORTH_AMERICA | _LATIN_AMERICA),
    ("Middle East", (), _MIDDLE_EAST),
    ("MENA", ("middle east and north africa",), _MIDDLE_EAST | {"MA"}),
    ("Africa", (), _AFRICA),
    ("ANZ", (), frozenset({"AU", "NZ"})),
    ("DACH", (), frozenset({"DE", "AT", "CH"})),
    ("Nordics", ("nordic",), frozenset({"DK", "FI", "NO", "SE", "IS"})),
    ("Benelux", (), frozenset({"BE", "NL", "LU"})),
)
_ZONE_MEMBERS: dict[str, frozenset[str]] = {}
_ZONE_KEYS: dict[str, str] = {}
for _zone, _aliases, _members in _ZONE_ROWS:
    _ZONE_MEMBERS[_zone] = _members
    _ZONE_KEYS[_zone.lower()] = _zone
    for _alias in _aliases:
        _ZONE_KEYS[_alias] = _zone

# display name, country, region, aliases. The first alias is the lowercase display name.
_CITY_ROWS: tuple[tuple[str, str, str | None, tuple[str, ...]], ...] = (
    ("New York", "US", "NY", ("new york", "new york city", "nyc", "brooklyn")),
    ("San Francisco", "US", "CA", ("san francisco", "san francisco bay area", "sf bay area",
                                   "bay area", "sf")),
    ("Los Angeles", "US", "CA", ("los angeles",)),
    ("Seattle", "US", "WA", ("seattle",)),
    ("Austin", "US", "TX", ("austin",)),
    ("Boston", "US", "MA", ("boston",)),
    ("Chicago", "US", "IL", ("chicago",)),
    ("Denver", "US", "CO", ("denver",)),
    ("Atlanta", "US", "GA", ("atlanta",)),
    ("Washington", "US", "DC", ("washington dc",)),
    ("San Jose", "US", "CA", ("san jose",)),
    ("Oakland", "US", "CA", ("oakland",)),
    ("Palo Alto", "US", "CA", ("palo alto",)),
    ("Mountain View", "US", "CA", ("mountain view",)),
    ("Menlo Park", "US", "CA", ("menlo park",)),
    ("Sunnyvale", "US", "CA", ("sunnyvale",)),
    ("Redwood City", "US", "CA", ("redwood city",)),
    ("San Mateo", "US", "CA", ("san mateo",)),
    ("Santa Monica", "US", "CA", ("santa monica",)),
    ("San Diego", "US", "CA", ("san diego",)),
    ("Dallas", "US", "TX", ("dallas",)),
    ("Houston", "US", "TX", ("houston",)),
    ("Miami", "US", "FL", ("miami",)),
    ("Philadelphia", "US", "PA", ("philadelphia",)),
    ("Pittsburgh", "US", "PA", ("pittsburgh",)),
    ("Minneapolis", "US", "MN", ("minneapolis",)),
    ("Raleigh", "US", "NC", ("raleigh",)),
    ("Boulder", "US", "CO", ("boulder",)),
    ("Nashville", "US", "TN", ("nashville",)),
    ("Salt Lake City", "US", "UT", ("salt lake city",)),
    ("Toronto", "CA", "ON", ("toronto",)),
    ("Ottawa", "CA", "ON", ("ottawa",)),
    ("Vancouver", "CA", "BC", ("vancouver",)),
    ("Montreal", "CA", "QC", ("montreal",)),
    ("Calgary", "CA", "AB", ("calgary",)),
    ("London", "GB", None, ("london",)),
    ("Manchester", "GB", None, ("manchester",)),
    ("Edinburgh", "GB", None, ("edinburgh",)),
    ("Dublin", "IE", None, ("dublin",)),
    ("Paris", "FR", None, ("paris",)),
    ("Lyon", "FR", None, ("lyon",)),
    ("Berlin", "DE", None, ("berlin",)),
    ("Munich", "DE", None, ("munich", "munchen")),
    ("Hamburg", "DE", None, ("hamburg",)),
    ("Frankfurt", "DE", None, ("frankfurt",)),
    ("Amsterdam", "NL", None, ("amsterdam",)),
    ("Madrid", "ES", None, ("madrid",)),
    ("Barcelona", "ES", None, ("barcelona",)),
    ("Lisbon", "PT", None, ("lisbon",)),
    ("Rome", "IT", None, ("rome",)),
    ("Milan", "IT", None, ("milan",)),
    ("Stockholm", "SE", None, ("stockholm",)),
    ("Copenhagen", "DK", None, ("copenhagen",)),
    ("Helsinki", "FI", None, ("helsinki",)),
    ("Vienna", "AT", None, ("vienna",)),
    ("Brussels", "BE", None, ("brussels",)),
    ("Warsaw", "PL", None, ("warsaw",)),
    ("Prague", "CZ", None, ("prague",)),
    ("Oslo", "NO", None, ("oslo",)),
    ("Zurich", "CH", None, ("zurich",)),
    ("Geneva", "CH", None, ("geneva",)),
    ("Sydney", "AU", None, ("sydney",)),
    ("Melbourne", "AU", None, ("melbourne",)),
    ("Auckland", "NZ", None, ("auckland",)),
    ("Bangalore", "IN", None, ("bangalore", "bengaluru")),
    ("Mumbai", "IN", None, ("mumbai",)),
    ("Delhi", "IN", None, ("delhi", "new delhi")),
    ("Hyderabad", "IN", None, ("hyderabad",)),
    ("Pune", "IN", None, ("pune",)),
    ("Singapore", "SG", None, ("singapore",)),
    ("Tel Aviv", "IL", None, ("tel aviv",)),
    ("Mexico City", "MX", None, ("mexico city",)),
    ("Sao Paulo", "BR", None, ("sao paulo",)),
    ("Rio de Janeiro", "BR", None, ("rio de janeiro",)),
    ("Tokyo", "JP", None, ("tokyo",)),
    ("Osaka", "JP", None, ("osaka",)),
    ("Shanghai", "CN", None, ("shanghai",)),
    ("Beijing", "CN", None, ("beijing",)),
    ("Shenzhen", "CN", None, ("shenzhen",)),
    ("Seoul", "KR", None, ("seoul",)),
    ("Taipei", "TW", None, ("taipei",)),
    ("Manila", "PH", None, ("manila",)),
    ("Bangkok", "TH", None, ("bangkok",)),
    ("Jakarta", "ID", None, ("jakarta",)),
    ("Kuala Lumpur", "MY", None, ("kuala lumpur",)),
    ("Ho Chi Minh City", "VN", None, ("ho chi minh city", "ho chi minh", "saigon")),
    ("Dubai", "AE", None, ("dubai",)),
    ("Abu Dhabi", "AE", None, ("abu dhabi",)),
    ("Riyadh", "SA", None, ("riyadh",)),
    ("Istanbul", "TR", None, ("istanbul",)),
    ("Cairo", "EG", None, ("cairo",)),
    ("Lagos", "NG", None, ("lagos",)),
    ("Nairobi", "KE", None, ("nairobi",)),
    ("Johannesburg", "ZA", None, ("johannesburg",)),
    ("Cape Town", "ZA", None, ("cape town",)),
    ("Buenos Aires", "AR", None, ("buenos aires",)),
    ("Santiago", "CL", None, ("santiago",)),
    ("Bogota", "CO", None, ("bogota",)),
    ("Medellin", "CO", None, ("medellin",)),
    ("Lima", "PE", None, ("lima",)),
    ("Montevideo", "UY", None, ("montevideo",)),
    ("Kyiv", "UA", None, ("kyiv", "kiev")),
)
_DISPLAY_OVERRIDES = {"Sao Paulo": "São Paulo"}
_CITIES: dict[str, tuple[str, str, str | None]] = {}
for _display, _country, _region, _aliases in _CITY_ROWS:
    for _alias in _aliases:
        _CITIES[_alias] = (_DISPLAY_OVERRIDES.get(_display, _display), _country, _region)

_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
_KEYWORDS = re.compile(r"\b(?:remote|hybrid|on[- ]?site|in[- ]office)\b", re.IGNORECASE)
_PAREN = re.compile(r"\([^)]*\)")
_SEPARATOR = re.compile(r"\||;|\u2022|\u00b7|\s/\s|\sor\s", re.IGNORECASE)
# "Remote in the US" leaves "in the US" once the keyword is dropped: read the place after it.
_LEAD_IN = re.compile(r"^(?:based\s+in|within(?:\s+the)?|in(?:\s+the)?)\s+(?=\S)", re.IGNORECASE)
_EDGE = " \t-–—:,;/."

_Found = tuple[str | None, str | None, str | None]  # country, region, city


def _norm(text: str) -> str:
    """Lowercase, accents and periods removed, spaces collapsed: the key for every lookup."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().replace(".", "").split())


def _unlead(token: str) -> str:
    """A comma token without a leading "in", "in the", "within (the)" or "based in"."""
    return _LEAD_IN.sub("", token.strip(), count=1).strip()


def _prettify(token: str) -> str:
    token = token.strip()
    return token.title() if token.islower() or token.isupper() else token


def _split(text: str) -> list[str]:
    """Split on the separators, but never inside parentheses."""
    depth_at = []
    depth = 0
    for char in text:
        depth_at.append(depth)
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
    pieces: list[str] = []
    start = 0
    for match in _SEPARATOR.finditer(text):
        if depth_at[match.start()] == 0:
            pieces.append(text[start:match.start()])
            start = match.end()
    pieces.append(text[start:])
    return [p.strip() for p in pieces if p.strip()]


def _clean(text: str) -> str:
    return " ".join(_KEYWORDS.sub(" ", text).split()).strip(_EDGE).strip()


def _candidates(key: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Region options (country, code) and country options for one normalized token."""
    regions: list[tuple[str, str]] = []
    if key in _US_STATES:
        regions.append(("US", _US_STATES[key]))
    elif len(key) == 2 and key.upper() in _US_CODES:
        regions.append(("US", key.upper()))
    if key in _CA_PROVINCES:
        regions.append(("CA", _CA_PROVINCES[key]))
    elif len(key) == 2 and key.upper() in _CA_CODES:
        regions.append(("CA", key.upper()))
    countries = [_COUNTRY_KEYS[key]] if key in _COUNTRY_KEYS else []
    return regions, countries


def _city_display(token: str, key: str, country: str | None) -> str:
    row = _CITIES.get(key)
    if row is not None and (country is None or row[1] == country):
        return row[0]
    return _prettify(token)


def _resolve_single(token: str, key: str) -> _Found | None:
    row = _CITIES.get(key)
    if row is not None:
        return row[1], row[2], row[0]
    regions, countries = _candidates(key)
    options: list[tuple[str, str | None]] = [*regions, *((c, None) for c in countries)]
    if len(set(options)) == 1:
        country, region = options[0]
        return country, region, None
    return None


def _resolve(text: str, budget: int = _MAX_PIECES) -> tuple[list[_Found], list[str]]:
    """Every place in one comma list, in input order, and the parts of it that could not be read.

    The last tokens give the right-most city; whatever sits left of that city is read the same
    way, so "New York, NY, London, UK" gives both. A left part nobody can pair (a street, a bare
    "Phoenix") comes back in the second list. `budget` caps the places one list may give;
    shortcut: parts past the cap are dropped unreported, fine for text already capped at
    _MAX_TEXT.
    """
    tokens = [t for t in (_unlead(t) for t in text.split(",") if t.strip()) if t]
    if not tokens:
        return [], []
    keys = [_norm(t) for t in tokens]
    if len(tokens) == 1:
        single = _resolve_single(tokens[0], keys[0])
        return ([single], []) if single else ([], [tokens[0]])

    last = len(tokens) - 1
    city_row = _CITIES.get(keys[last - 1])
    regions, countries = _candidates(keys[last])
    # After a city: a US state first, then a country, then a Canadian province.
    options: list[tuple[str, str | None]] = (
        [r for r in regions if r[0] == "US"] + [(c, None) for c in countries]
        + [r for r in regions if r[0] != "US"])
    if city_row is not None:
        agreeing = [o for o in options if o[0] == city_row[1]]
        if (not agreeing and options and city_row[1] != "US" and options[0][0] == "US"
                and options[0][1] in _SHARED_REGION_CODES.get(city_row[1], frozenset())):
            # "Amsterdam, NH": NH is a US state and also a region of the city's own country, so
            # the place is unknown and the title decides. "Dublin, OH" stays a US town.
            return [], [text]
        options = agreeing or options
    if not options:
        return [], [text]
    country, region = options[0]
    index = last - 1
    if region is None:
        # A country was read. Skip repeats of it (England, then United Kingdom), then take a
        # state or province of that country when one sits next to it.
        while index >= 0 and country in _candidates(keys[index])[1]:
            index -= 1
        if index >= 0:
            own = [r for r in _candidates(keys[index])[0] if r[0] == country]
            if own:
                region = own[0][1]
                index -= 1
    city: str | None = None
    if index >= 0:
        city = _city_display(tokens[index], keys[index], country)
        row = _CITIES.get(keys[index])
        if region is None and row is not None and row[1] == country:
            region = row[2]
    before: list[_Found] = []
    unread: list[str] = []
    if index > 0 and budget > 1:
        before, unread = _resolve(", ".join(tokens[:index]), budget - 1)
    return [*before, (country, region, city)], unread


def _country_list(text: str) -> list[str] | None:
    """The countries of a comma list made only of countries ("US, Canada"), in input order.

    None unless there are two or more tokens and every token is a country and nothing else
    (not a city, a US state or a Canadian province), so "Berlin, DE" and "Toronto, ON, Canada"
    keep their city-then-country reading.
    """
    tokens = [t for t in (_unlead(t) for t in text.split(",") if t.strip()) if t]
    if len(tokens) < 2:
        return None
    keys = [_norm(t) for t in tokens]
    for key in keys:
        if key not in _COUNTRY_KEYS or key in _CITIES or _candidates(key)[0]:
            return None
    return list(dict.fromkeys(_COUNTRY_KEYS[k] for k in keys))


def _parse_piece(piece: str, allow_hints: bool, unread: list[str] | None = None) -> list[Place]:
    remote = bool(_REMOTE.search(piece))
    hints = [h[1:-1] for h in _PAREN.findall(piece)]
    cleaned = _clean(_PAREN.sub(" ", piece))
    if cleaned:
        zone = _ZONE_KEYS.get(_norm(_unlead(cleaned)))
        if zone is not None:
            return [Place(None, None, None, remote, zone)]
        countries = _country_list(cleaned)
        if countries is not None:
            return [Place(c, None, None, remote) for c in countries]
        found, left = _resolve(cleaned)
        if found:
            if unread is not None:
                unread.extend(left)
            return [Place(c, r, city, remote) for c, r, city in found]
    if allow_hints:
        for hint in hints:
            hint_text = _clean(hint)
            if not hint_text:
                continue
            places = [p for part in _split(hint_text)[:_MAX_PIECES]
                      for p in _parse_piece(part, allow_hints=False)]
            places = [p for p in places if p.country or p.region or p.city or p.zone]
            if places:
                return [Place(p.country, p.region, p.city, p.remote or remote, p.zone)
                        for p in places]
    if cleaned and unread is not None:
        unread.append(cleaned)
    return [Place(None, None, None, True)] if remote else []


def region_places(code: str) -> list[Place]:
    """Places for a region code the user allowed ("CA", "DE", "EU", "UK"), country only.

    parse_location refuses a bare two-letter code that could be a US state or a Canadian
    province, which is right for posting text but wrong for a code the user chose from the
    country list. "EU" is every member country. Empty when the text is not such a code.
    """
    if not isinstance(code, str):
        return []
    key = code.strip().upper()
    if key == "EU":
        return [Place(c, None, None, False) for c in sorted(EU_COUNTRIES)]
    key = "GB" if key == "UK" else key
    if key in _COUNTRY_NAMES:
        return [Place(key, None, None, False)]
    return []


def _dedupe(places: list[Place]) -> list[Place]:
    seen: set[Place] = set()
    kept: list[Place] = []
    for place in places:
        if place not in seen:
            seen.add(place)
            kept.append(place)
    return kept


def read_places(texts: list[str]) -> tuple[list[Place], list[str]]:
    """Places from several strings, deduplicated in input order, and the parts nobody could read."""
    places: list[Place] = []
    unread: list[str] = []
    for text in texts:
        if isinstance(text, str):
            pieces = _split(text[:_MAX_TEXT])[:_MAX_PIECES]
            places += [p for piece in pieces for p in _parse_piece(piece, True, unread)]
    return _dedupe(places), unread


def parse_location(text: str) -> list[Place]:
    """Places named in one location string; empty when nothing in it can be resolved."""
    return read_places([text])[0]


def parse_locations(texts: list[str]) -> list[Place]:
    """Places from several strings, flattened and deduplicated, in input order."""
    return read_places(texts)[0]


def user_place(text: str) -> tuple[list[Place], list[str]]:
    """One place the user typed, a location or a region code, and the parts that could not be read."""
    places, unread = read_places([text])
    codes = [] if places else region_places(text)
    return places or codes, [] if codes else unread


_COUNTRY_DISPLAY = {"US": "United States", "GB": "United Kingdom", "CZ": "Czechia",
                    "NL": "the Netherlands"}


def place_label(place: Place) -> str:
    """A short plain name for a place, for a reason sentence: city, else zone, else country."""
    if place.city:
        return place.city
    if place.zone:
        return place.zone
    if place.country:
        names = _COUNTRY_NAMES.get(place.country, ())
        return _COUNTRY_DISPLAY.get(place.country) or (
            names[0].title() if names else place.country)
    return "an unstated place"


# Title phrases: zone names and aliases, country names and aliases of four or more characters
# (so no two-letter code), and city aliases of four or more characters. Built once from the
# tables above; nothing here comes from config or posting text.
_TITLE_MIN = 4


def _title_phrases() -> dict[str, Place]:
    found: dict[str, Place] = {}
    for key, name in _COUNTRY_KEYS.items():
        if len(key) >= _TITLE_MIN:
            found[key] = Place(name, None, None, False)
    for key, (display, country, region) in _CITIES.items():
        if len(key) >= _TITLE_MIN:
            found[key] = Place(country, region, display, False)
    for key, zone in _ZONE_KEYS.items():
        found[key] = Place(None, None, None, False, zone)
    return found


_TITLE_PHRASES = _title_phrases()
_TITLE_NAMED = re.compile(
    r"(?<!\w)(?:" + "|".join(
        r"\s+".join(re.escape(word) for word in key.split())
        for key in sorted(_TITLE_PHRASES, key=lambda k: (-len(k), k))) + r")(?!\w)",
    re.IGNORECASE)
# The only short tokens read, and only in capitals: "Account Executive, US" but never "us".
# A US state named right after a place ("Lima, Ohio", "Cairo, IL"), and the word before a place
# that makes a state or province name ("New" + "Mexico"). Either makes the phrase a US place (or
# part of a North American name) that shares its name with a foreign one, so it is not read
# A Canadian province after a place ("Toronto, ON") is Canada, not a US place, so it is
# read as one; "CA" is California, so it stays a US doubt.
_STATE_NAMES = sorted(_US_STATES, key=lambda k: (-len(k), k))
_STATE_AFTER = re.compile(
    r"\s*,?\s*(?:(?:" + "|".join(r"\s+".join(map(re.escape, k.split())) for k in _STATE_NAMES)
    + r")(?!\w)|(?-i:" + "|".join(sorted(_US_CODES)) + r")(?!\w))", re.IGNORECASE)
_PROVINCE_NAMES = sorted(_CA_PROVINCES, key=lambda k: (-len(k), k))
_PROVINCE_AFTER = re.compile(
    r"\s*(,?)\s*(?:(" + "|".join(r"\s+".join(map(re.escape, k.split())) for k in _PROVINCE_NAMES)
    + r")(?!\w)|(?-i:(" + "|".join(sorted(_CA_CODES)) + r"))(?!\w))", re.IGNORECASE)
_WORD_BEFORE = re.compile(r"(\w+)\s+$")
_TITLE_CAPS = re.compile(r"(?<!\w)(US|USA|UK)(?!\w)")
_CAPS_PLACES = {"US": Place("US", None, None, False), "USA": Place("US", None, None, False),
                "UK": Place("GB", None, None, False)}


def _is_north_american(text: str, start: int, end: int, phrase: str) -> bool:
    """True when a title phrase is really a US state or Canadian province ("New Mexico") or a
    place followed by a US state ("Lima, Ohio", "Cairo, IL"). Any doubt reads as no place."""
    before = _WORD_BEFORE.search(text[:start])
    if before is not None:
        joined = f"{before.group(1).casefold()} {phrase}"
        if joined in _US_STATES or joined in _CA_PROVINCES:
            return True
    return _STATE_AFTER.match(text, end) is not None


def _canadian_reading(text: str, end: int, place: Place) -> Place | None | bool:
    """What a Canadian province right after a title place does to it.

    Returns None when no province follows. A place already in Canada is read as it is. A city
    elsewhere, with the province after a comma ("London, ON", "Paris, Ontario"), is the Canadian
    city of that name. Anything else (a country, a zone, a province with no comma) is doubtful
    and returns False, so it is not read.
    """
    found = _PROVINCE_AFTER.match(text, end)
    if found is None:
        return None
    if place.country == "CA":
        return place
    if place.city is None or not found.group(1):
        return False
    code = found.group(3) or _CA_PROVINCES[" ".join(found.group(2).casefold().split())]
    return Place("CA", code, place.city, False)


def title_places(title: str) -> list[Place]:
    """Places a posting title names, in title order and without repeats; empty when none.

    Used only when the locations give no country and no zone. Whole words only, longest phrase
    first, and each matched span is used up, so "North America" is never also "America". Never
    reads a two-letter code other than the capitals US and UK, and never a US state or Canadian
    province name, so "IT Program Manager" and "Manager, Washington" name no place. A phrase that
    makes a state ("New Mexico") or is followed by a US state ("Lima, Ohio", "Cairo, IL") is not
    read either, so a US place that shares a foreign name never rejects. A phrase followed
    by a Canadian province is Canada ("Toronto, ON", "London, ON").
    """
    if not isinstance(title, str):
        return []
    text = unicodedata.normalize("NFKD", title[:_MAX_TITLE])
    text = "".join(c for c in text if not unicodedata.combining(c))
    spans: list[tuple[int, int, Place]] = []
    for match in _TITLE_NAMED.finditer(text):
        phrase = " ".join(match.group(0).casefold().split())
        place = _TITLE_PHRASES.get(phrase)
        if place is None or _is_north_american(text, match.start(), match.end(), phrase):
            continue
        canadian = _canadian_reading(text, match.end(), place)
        if canadian is not False:
            spans.append((match.start(), match.end(), canadian or place))
    for match in _TITLE_CAPS.finditer(text):
        spans.append((match.start(), match.end(), _CAPS_PLACES[match.group(0)]))
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    places: list[Place] = []
    used_to = -1
    for start, end, place in spans:
        if start >= used_to:
            places.append(place)
            used_to = end
    return _dedupe(places)


def in_regions(places: list[Place], allow: list[str]) -> bool | None:
    """True when any place is in an allowed country (or "EU"), False when none can be,
    None when the places do not say.

    A place with a country is inside or outside. A zone place is outside when none of its member
    countries is allowed and unknown when one is (the role may or may not be in the allowed
    part). A place with neither country nor zone (a bare "Remote") is unknown.
    """
    allowed = {("GB" if a.strip().upper() == "UK" else a.strip().upper()) for a in allow
               if a.strip()}
    if not allowed or not places:
        return None
    reach = allowed | EU_COUNTRIES if "EU" in allowed else allowed
    if any(p.country in reach for p in places if p.country is not None):
        return True
    for place in places:
        if place.country is not None:
            continue
        members = _ZONE_MEMBERS.get(place.zone) if place.zone is not None else None
        if members is None or members & reach:
            return None
    return False


def _same_place(posting: Place, user: Place) -> bool:
    if user.city is not None:
        if posting.city is None or _norm(posting.city) != _norm(user.city):
            return False
        if posting.country and user.country and posting.country != user.country:
            return False
        return not (posting.region and user.region and posting.region != user.region)
    if user.region is not None:
        return posting.country == user.country and posting.region == user.region
    if user.country is not None:
        return posting.country == user.country
    return False


def place_matches(posting_places: list[Place], user_places: list[Place]) -> bool:
    """True when any posting place is any of the user's places. False when either list is
    empty. A user place with only a region matches a posting place in that region."""
    if not posting_places or not user_places:
        return False
    return any(_same_place(p, u) for p in posting_places for u in user_places)
