"""Rule-based location normalisation. No AI, no network: small alias tables, India first."""

import re
from dataclasses import dataclass, field

# alias (lowercase) -> (canonical city, ISO-3166 alpha-2 country)
_CITY_ROWS: dict[str, list[str]] = {
    # India
    "Bengaluru|IN": ["bengaluru", "bangalore"],
    "Gurugram|IN": ["gurugram", "gurgaon"],
    "Mumbai|IN": ["mumbai", "bombay", "navi mumbai"],
    "Delhi|IN": ["delhi", "new delhi"],
    "Noida|IN": ["noida"],
    "Hyderabad|IN": ["hyderabad"],
    "Chennai|IN": ["chennai", "madras"],
    "Pune|IN": ["pune"],
    "Kolkata|IN": ["kolkata", "calcutta"],
    "Ahmedabad|IN": ["ahmedabad"],
    "Kochi|IN": ["kochi", "cochin"],
    "Jaipur|IN": ["jaipur"],
    "Chandigarh|IN": ["chandigarh"],
    "Indore|IN": ["indore"],
    "Coimbatore|IN": ["coimbatore"],
    "Thiruvananthapuram|IN": ["thiruvananthapuram", "trivandrum"],
    "Bhubaneswar|IN": ["bhubaneswar"],
    "Nagpur|IN": ["nagpur"],
    "Lucknow|IN": ["lucknow"],
    # Rest of the world (large hubs)
    "London|GB": ["london"],
    "Manchester|GB": ["manchester"],
    "New York|US": ["new york", "nyc"],
    "San Francisco|US": ["san francisco"],
    "Seattle|US": ["seattle"],
    "Austin|US": ["austin"],
    "Boston|US": ["boston"],
    "Chicago|US": ["chicago"],
    "Los Angeles|US": ["los angeles"],
    "Toronto|CA": ["toronto"],
    "Vancouver|CA": ["vancouver"],
    "Berlin|DE": ["berlin"],
    "Munich|DE": ["munich"],
    "Amsterdam|NL": ["amsterdam"],
    "Dublin|IE": ["dublin"],
    "Paris|FR": ["paris"],
    "Madrid|ES": ["madrid"],
    "Barcelona|ES": ["barcelona"],
    "Warsaw|PL": ["warsaw"],
    "Zurich|CH": ["zurich"],
    "Stockholm|SE": ["stockholm"],
    "Singapore|SG": ["singapore"],
    "Sydney|AU": ["sydney"],
    "Melbourne|AU": ["melbourne"],
    "Tokyo|JP": ["tokyo"],
    "Dubai|AE": ["dubai"],
    "Tel Aviv|IL": ["tel aviv"],
    "Sao Paulo|BR": ["sao paulo", "são paulo"],
    "Mexico City|MX": ["mexico city"],
    "Mountain View|US": ["mountain view"],
    "Palo Alto|US": ["palo alto"],
    "Bellevue|US": ["bellevue"],
    "Sunnyvale|US": ["sunnyvale"],
    "San Jose|US": ["san jose"],
    "Santa Clara|US": ["santa clara"],
    "Belgrade|RS": ["belgrade"],
    "Seoul|KR": ["seoul"],
    "Manila|PH": ["manila"],
    "Copenhagen|DK": ["copenhagen"],
    "Milan|IT": ["milan"],
    "Kuala Lumpur|MY": ["kuala lumpur"],
    "Jakarta|ID": ["jakarta"],
    "Johannesburg|ZA": ["johannesburg"],
    "Montreal|CA": ["montreal"],
}

_CITY_ALIASES: dict[str, tuple[str, str]] = {}
for _key, _aliases in _CITY_ROWS.items():
    _canonical, _country = _key.split("|")
    for _alias in _aliases:
        _CITY_ALIASES[_alias] = (_canonical, _country)

# Long country names are safe to find anywhere in the text.
_COUNTRY_NAMES: dict[str, str] = {
    "india": "IN",
    "united states": "US",
    "united kingdom": "GB",
    "canada": "CA",
    "germany": "DE",
    "netherlands": "NL",
    "ireland": "IE",
    "france": "FR",
    "spain": "ES",
    "poland": "PL",
    "switzerland": "CH",
    "sweden": "SE",
    "singapore": "SG",
    "australia": "AU",
    "japan": "JP",
    "israel": "IL",
    "brazil": "BR",
    "mexico": "MX",
    "united arab emirates": "AE",
    "serbia": "RS",
    "luxembourg": "LU",
    "south korea": "KR",
    "korea": "KR",
    "philippines": "PH",
    "italy": "IT",
    "denmark": "DK",
    "costa rica": "CR",
    "new zealand": "NZ",
    "romania": "RO",
    "hong kong": "HK",
    "taiwan": "TW",
    "malaysia": "MY",
    "indonesia": "ID",
    "vietnam": "VN",
    "china": "CN",
    "south africa": "ZA",
    "portugal": "PT",
    "belgium": "BE",
    "austria": "AT",
    "uruguay": "UY",
    "argentina": "AR",
    "colombia": "CO",
    "estonia": "EE",
}
# US states that are not also common city or country names ("Washington", "Georgia" are left out).
_US_STATES = [
    "california", "texas", "virginia", "maryland", "illinois", "pennsylvania", "colorado",
    "florida", "massachusetts", "new jersey", "north carolina", "south carolina", "oregon",
    "arizona", "minnesota", "ohio", "michigan", "tennessee", "iowa", "connecticut", "utah",
]  # fmt: skip
_COUNTRY_NAMES.update({state: "US" for state in _US_STATES})
# Short forms are ambiguous ("in", "us"), so they only count when they are a whole comma-part.
_COUNTRY_SHORT: dict[str, str] = {
    "in": "IN", "ind": "IN", "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US",
    "uk": "GB", "gb": "GB", "ca": "CA", "de": "DE", "sg": "SG", "au": "AU", "ae": "AE", "uae": "AE",
    "cri": "CR", "cdmx": "MX",
}  # fmt: skip

_REMOTE = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed)\b", re.IGNORECASE)
# A bare "-" splits too, so "Remote-US" and "US-Remote" (seen on live boards) yield "US".
_PART_SPLIT = re.compile(r"[,;|/()\-–]|\bor\b|\band\b", re.IGNORECASE)


def _alternation(words: list[str]) -> re.Pattern[str]:
    ordered = sorted(words, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in ordered) + r")\b", re.IGNORECASE)


_CITY_RE = _alternation(list(_CITY_ALIASES))
_COUNTRY_RE = _alternation(list(_COUNTRY_NAMES))


@dataclass
class LocationInfo:
    cities: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    is_remote: bool = False


def _add(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def normalize_location(text: str | None) -> LocationInfo:
    info = LocationInfo()
    if not text:
        return info

    info.is_remote = bool(_REMOTE.search(text))

    for match in _CITY_RE.finditer(text):
        city, country = _CITY_ALIASES[match.group().lower()]
        _add(info.cities, city)
        _add(info.countries, country)

    for match in _COUNTRY_RE.finditer(text):
        _add(info.countries, _COUNTRY_NAMES[match.group().lower()])

    for part in _PART_SPLIT.split(text):
        code = _COUNTRY_SHORT.get(part.strip().lower())
        if code:
            _add(info.countries, code)

    return info
