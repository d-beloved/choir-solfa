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
