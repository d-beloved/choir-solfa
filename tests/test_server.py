"""Past songs are read back from their folders on disk."""
import json

from choir_solfa import server

A, B, C = "a" * 32, "b" * 32, "c" * 32


def _song(jobs, job_id, title, created, url=None):
    folder = jobs / job_id
    folder.mkdir(parents=True)
    redo = {"url": url, "mode": "auto", "meter": "auto", "title": ""} if url else None
    (folder / "job.json").write_text(json.dumps({"title": title, "created": created, "redo": redo}))
    (folder / "result.json").write_text(json.dumps({"title": title, "key": "Doh is Bb", "meter": "3/4",
                                                    "kind": "choir", "bars": 40, "polyphony": 0.4}))
    (folder / "score.pdf").write_bytes(b"%PDF")


def test_songs_are_listed_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JOBS", tmp_path)
    _song(tmp_path, A, "Older", 100.0, url="https://www.youtube.com/watch?v=x")
    _song(tmp_path, B, "Newer", 200.0)
    (tmp_path / C).mkdir()  # started but never finished
    (tmp_path / "not-a-job").mkdir()
    songs = server.list_songs()
    assert [s["title"] for s in songs] == ["Newer", "Older"]
    assert songs[1]["redo"]["url"].startswith("https://")
    assert songs[0]["redo"] is None and songs[0]["key"] == "Doh is Bb"


def test_only_known_hosts_are_answered():
    from fastapi.testclient import TestClient

    client = TestClient(server.create_app(4790, ["Solfa.Example.com"]))
    assert client.get("/", headers={"host": "127.0.0.1:4790"}).status_code == 200
    assert client.get("/", headers={"host": "solfa.example.com"}).status_code == 200
    assert client.get("/", headers={"host": "evil.example.com"}).status_code == 403
    assert client.delete(f"/api/songs/{A}", headers={"host": "solfa.example.com"}).status_code == 403  # no token


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_restarts_only_when_idle_after_a_song(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(server.time, "time", clock)
    jobs = server.Jobs()
    r = server.Restarter(jobs, quiet=120, stamp=lambda: 1.0, clock=clock)
    assert r.reason() is None  # nothing happened yet
    jobs.add(A, title="x")
    jobs.update(A, status="working")
    clock.now += 600
    assert r.reason() is None  # never during a song
    jobs.update(A, status="done")
    clock.now += 60
    assert r.reason() is None  # the page may still be fetching the score
    clock.now += 61
    assert "song" in r.reason()
    r.touch()  # someone opened the page
    assert r.reason() is None


def test_restarts_after_code_settles():
    clock = Clock()
    stamp = [1.0]
    r = server.Restarter(server.Jobs(), quiet=120, settle=10, stamp=lambda: stamp[0], clock=clock)
    clock.now += 200
    assert r.reason() is None
    stamp[0] = clock.now - 3  # just saved a file
    assert r.reason() is None
    clock.now += 10
    assert r.reason() == "the code changed"


def test_token_survives_restarts_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "HOME", tmp_path)
    first = server._token(persist=True)
    assert server._token(persist=True) == first
    assert oct((tmp_path / "token").stat().st_mode & 0o777) == "0o600"
    assert server._token(persist=False) != first


def test_a_forgotten_job_comes_back_from_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JOBS", tmp_path)
    _song(tmp_path, A, "Silent Night", 100.0)
    (tmp_path / C).mkdir()
    jobs = server.Jobs()
    done = jobs.get(A)
    assert done["status"] == "done" and done["result"]["title"] == "Silent Night"
    assert "polyphony" not in done["result"]
    assert jobs.get(C)["status"] == "failed"
    assert jobs.get(B) is None
    assert server._job_from_disk("../etc") is None


def test_desktop_app_quits_only_when_no_page_is_open():
    jobs = server.Jobs()
    clock = Clock()
    closer = server.Closer(jobs, after=900, clock=clock)
    clock.now += 899
    assert closer.reason() is None
    closer.touch()  # an open page pings
    clock.now += 899
    assert closer.reason() is None
    clock.now += 2
    assert "15 minutes" in closer.reason()
    jobs.add("busy")
    assert closer.reason() is None  # never while a song is being made


def test_ping_keeps_desktop_app_but_not_restart_alive(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "HOME", tmp_path)  # the restart mode saves its token there
    app = server.create_app(4790, restart_when_idle=True, quit_when_closed=True)
    client = TestClient(app, base_url="http://127.0.0.1:4790")
    app.state.restarter.last_request = 0
    app.state.closer.last_seen = 0
    assert client.get("/api/ping").json() == {"app": "choir-solfa"}
    assert app.state.restarter.last_request == 0
    assert app.state.closer.last_seen > 0
