from jobagent.locations import normalize_location as n


def test_alias_and_country():
    info = n("Bangalore, India")
    assert info.cities == ["Bengaluru"] and info.countries == ["IN"] and not info.is_remote


def test_multi_city_and_remote():
    info = n("Pune or Hyderabad / Remote")
    assert info.cities == ["Pune", "Hyderabad"] and info.is_remote


def test_short_country_only_as_whole_part():
    assert n("Remote, US").countries == ["US"]
    assert n("Berlin, DE").countries == ["DE"]
    assert n("Plain text in office").countries == []


def test_empty():
    assert n(None).cities == [] and n("").countries == []


def test_hyphenated_remote_country():
    # Live Greenhouse strings: a US-only remote job must not look like "remote anywhere".
    for text in ("SF, SEA, NY, Remote-US", "US-Remote", "US-REM"):
        info = n(text)
        assert info.countries == ["US"], text
    assert n("Bengaluru-VTP, India").cities == ["Bengaluru"]


def test_more_countries_and_us_states():
    assert n("Belgrade, Serbia").countries == ["RS"]
    assert n("Remote - Luxembourg").countries == ["LU"] and n("Remote - Luxembourg").is_remote
    assert n("Mountain View, California").countries == ["US"]
    assert n("CRI - Remote").countries == ["CR"]
    assert n("India Locations").countries == ["IN"]
    assert n("Indiana").countries == []  # "india" is a whole-word match only
