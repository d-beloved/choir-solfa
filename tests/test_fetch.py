"""Spotify link handling (no network)."""
import pytest

from choir_solfa.fetch import (FetchError, SpotifyTrack, is_spotify, is_url, parse_spotify_page,
                               score_candidate, spotify_kind_and_id)

PAGE = '''<meta property="og:title" content="Silent Night"/>
<meta property="og:description" content="Franz Xaver Gruber, Choir of King&#x27;s College, Cambridge · Carols · Song · 1998"/>
<meta name="music:duration" content="156"/>
<meta name="music:musician_description" content="Franz Xaver Gruber, Choir of King&#x27;s College, Cambridge"/>'''


def test_spotify_links_are_recognised():
    assert is_spotify("https://open.spotify.com/track/7nR6yC5uUjJ7AcP78kOGae?si=x")
    assert is_spotify("spotify:track:7nR6yC5uUjJ7AcP78kOGae")
    assert is_url("spotify:track:7nR6yC5uUjJ7AcP78kOGae")
    assert not is_spotify("https://www.youtube.com/watch?v=abc")
    assert spotify_kind_and_id("https://open.spotify.com/intl-fr/track/7nR6yC5uUjJ7AcP78kOGae") == ("track", "7nR6yC5uUjJ7AcP78kOGae")
    assert spotify_kind_and_id("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")[0] == "playlist"
    with pytest.raises(FetchError):
        spotify_kind_and_id("https://open.spotify.com/")


def test_spotify_page_details():
    t = parse_spotify_page(PAGE, "x")
    assert (t.title, t.duration) == ("Silent Night", 156)
    assert "King's College" in t.artist


def test_candidate_ranking_prefers_same_recording():
    t = SpotifyTrack("x", "Silent Night", "Franz Xaver Gruber, Choir of King's College, Cambridge", 156)
    same = {"title": "Silent Night", "channel": "KingsCollegeChoir", "duration": 158}
    other_choir = {"title": "Silent Night", "channel": "Some Parish Choir", "duration": 156}
    karaoke = {"title": "Silent Night (Karaoke)", "channel": "KingsCollegeChoir", "duration": 156}
    longer = {"title": "Silent Night", "channel": "KingsCollegeChoir", "duration": 220}
    costs = {k: score_candidate(t, v) for k, v in
             dict(same=same, other_choir=other_choir, karaoke=karaoke, longer=longer).items()}
    assert costs["same"] < costs["other_choir"] < costs["karaoke"]
    assert costs["same"] < costs["longer"]


def test_youtube_refusal_gets_plain_words():
    from choir_solfa import fetch

    bot = Exception("ERROR: [youtube] CZmC4qqL_Rg: Sign in to confirm you’re not a bot. This helps protect our community.")
    assert fetch._refused(bot)
    assert not fetch._refused(Exception("ERROR: Video unavailable"))
    assert "Choose an audio file" in fetch.REFUSED


def test_links_use_the_proxy_when_one_is_set(monkeypatch):
    from choir_solfa import fetch
    monkeypatch.delenv("CHOIR_SOLFA_PROXY", raising=False)
    assert fetch._proxy() == {}
    monkeypatch.setenv("CHOIR_SOLFA_PROXY", "http://127.0.0.1:1080")
    assert fetch._proxy() == {"proxy": "http://127.0.0.1:1080"}


def test_spotify_pages_are_read_through_the_proxy_and_stay_on_spotify(monkeypatch):
    import urllib.error
    import urllib.request

    from choir_solfa import fetch
    monkeypatch.setenv("CHOIR_SOLFA_PROXY", "http://127.0.0.1:1080")
    built = []
    real_build = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: built.append(h) or real_build(*h))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda self, req, timeout=None: None)
    fetch._open(urllib.request.Request("https://open.spotify.com/track/x"))
    proxy = next(h for h in built[0] if isinstance(h, urllib.request.ProxyHandler))
    assert proxy.proxies == {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"}

    redirects = fetch._SpotifyRedirects()
    req = urllib.request.Request("https://spotify.link/abc")
    for good in ("https://open.spotify.com/track/x", "https://spotify.app.link/abc"):
        assert redirects.redirect_request(req, None, 302, "Found", {}, good) is not None
    for bad in ("http://open.spotify.com/track/x", "https://169.254.169.254/latest", "https://evil.example/spotify.com"):
        with pytest.raises(urllib.error.HTTPError):
            redirects.redirect_request(req, None, 302, "Found", {}, bad)
