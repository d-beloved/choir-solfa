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
