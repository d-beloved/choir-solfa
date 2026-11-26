"""Local web page: paste a link or drop a file, watch progress, download the score.

Bound to 127.0.0.1 only, with a Host check (stops DNS-rebinding pages from
reaching it) and a per-run token on every write request. The website version of
the same page runs on AWS instead (cloud/, infra/).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.request
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import practice

HOME = Path(os.environ.get("CHOIR_SOLFA_HOME", Path.home() / ".choir-solfa"))
JOBS = HOME / "jobs"
CODE = Path(__file__).parent
WEB = CODE / "web"
MAX_UPLOAD = 300 * 1024 * 1024
MAX_LYRICS = 10_000  # characters of pasted words
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
JOB_FILE = "job.json"  # how the song was asked for: link, settings, time


class Jobs:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict[str, dict] = {}
        self.pool = ThreadPoolExecutor(max_workers=1)  # one song at a time; the rest wait in line

    def add(self, job_id: str, **info) -> str:
        with self.lock:
            self.state[job_id] = {"id": job_id, "status": "waiting", "stage": "Waiting to start", "progress": 0.0,
                                  "created": time.time(), **info}
        return job_id

    def update(self, job_id: str, **fields) -> None:
        with self.lock:
            self.state[job_id].update(fields)

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.state.get(job_id)
            if job:
                return {k: v for k, v in job.items() if not k.startswith("_")}
        return _job_from_disk(job_id)

    def busy(self, job_id: str) -> bool:
        with self.lock:
            return self.state.get(job_id, {}).get("status") in ("waiting", "working")


def _token() -> str:
    return secrets.token_urlsafe(24)


def create_app(port: int) -> FastAPI:
    token = _token()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    jobs = Jobs()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def host_check(request: Request, call_next):
        if request.headers.get("host", "").lower() not in allowed_hosts:
            return JSONResponse({"error": "Wrong host."}, status_code=403)
        if request.method != "GET" and not secrets.compare_digest(request.headers.get("x-token", ""), token):
            return JSONResponse({"error": "Missing or wrong token. Reload the page."}, status_code=403)
        return await call_next(request)

    @app.get("/api/ping")
    def ping() -> dict:
        return {"app": "choir-solfa"}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        # The same page runs on AWS, where the deploy fills in __CLOUD__ (see infra/stack.py).
        return (WEB / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", token).replace("__CLOUD__", "null")

    @app.get("/privacy.html", response_class=HTMLResponse, include_in_schema=False)
    def privacy() -> str:
        return (WEB / "privacy.html").read_text(encoding="utf-8")

    icons = {"icon.png": "image/png", "logo.svg": "image/svg+xml", "apple-touch-icon.png": "image/png",
             "favicon.ico": "image/x-icon"}

    @app.get("/{name}", include_in_schema=False)
    def icon(name: str) -> FileResponse:
        if name not in icons:
            raise HTTPException(404)
        return FileResponse(CODE / "assets" / name, media_type=icons[name])

    @app.post("/api/jobs")
    async def create_job(
        url: str = Form(""), mode: str = Form("auto"), meter: str = Form("auto"), title: str = Form(""),
        lyrics: str = Form(""), file: UploadFile | None = File(None),
    ):
        from .fetch import is_url
        from .models import METERS

        if mode not in ("auto", "choir", "backing", "solo") or (meter != "auto" and meter not in METERS):
            raise HTTPException(400, "Unknown option.")
        if len(lyrics) > MAX_LYRICS:
            raise HTTPException(400, "Those words are too long. Paste the words of one song.")
        lyrics = lyrics.strip()
        job_id = uuid.uuid4().hex
        workdir = JOBS / job_id  # the folder keeps the score after the server stops
        upload_name = None
        if file is not None and file.filename:
            workdir.mkdir(parents=True, exist_ok=True)
            tmp = workdir / "upload.bin"
            size = 0
            with tmp.open("wb") as out:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        out.close()
                        shutil.rmtree(workdir, ignore_errors=True)
                        raise HTTPException(413, "That file is larger than 300 MB.")
                    out.write(chunk)
            source, upload_name = str(tmp), file.filename
        elif is_url(url):
            source = url.strip()
        else:
            raise HTTPException(400, "Paste a link to a song or choose a file.")

        workdir.mkdir(parents=True, exist_ok=True)
        redo = {"url": source, "mode": mode, "meter": meter, "title": title, "lyrics": lyrics} if is_url(source) else None
        info = {"title": title or upload_name or source, "source": source if is_url(source) else upload_name,
                "created": time.time(), "redo": redo}
        (workdir / JOB_FILE).write_text(json.dumps(info, indent=2), encoding="utf-8")
        jobs.add(job_id, **info)
        jobs.pool.submit(_run_job, jobs, job_id, source, workdir, mode, meter, title or None, upload_name, lyrics)
        return {"id": job_id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = jobs.get(_check(job_id))
        if not job:
            raise HTTPException(404, "No such job.")
        return job

    @app.get("/api/jobs/{job_id}/{name}")
    def job_file(job_id: str, name: str, download: int = 0):
        files = {"score.pdf": "application/pdf", "score.mid": "audio/midi", "score.txt": "text/plain; charset=utf-8",
                 **{track: "audio/mp4" for track in practice.TRACKS.values()}}
        if name not in files:
            raise HTTPException(404)
        job = jobs.get(_check(job_id)) or {}
        path = JOBS / job_id / name
        if not path.exists():
            raise HTTPException(404, "Not ready.")
        stem = _safe_name((job.get("result") or {}).get("title") or "score")
        part = {file: part for part, file in practice.TRACKS.items()}.get(name)
        filename = f"{stem} - {part}{Path(name).suffix}" if part else f"{stem}{Path(name).suffix}"
        disposition = "attachment" if download else "inline"
        return FileResponse(path, media_type=files[name],
                            headers={"Content-Disposition": f'{disposition}; filename="{filename}"'})

    @app.get("/api/songs")
    def songs():
        return list_songs()

    @app.delete("/api/songs/{job_id}")
    def remove_song(job_id: str):
        folder = JOBS / _check(job_id)
        if jobs.busy(job_id):
            raise HTTPException(409, "This song is still being made.")
        if not folder.is_dir():
            raise HTTPException(404, "No such song.")
        shutil.rmtree(folder, ignore_errors=True)
        with jobs.lock:
            jobs.state.pop(job_id, None)
        return {"removed": job_id}

    return app


def list_songs(limit: int = 200) -> list[dict]:
    """Finished scores, newest first, read from the job folders on disk."""
    found = []
    for marker in (JOBS.glob("*/result.json") if JOBS.exists() else []):
        job = _job_from_disk(marker.parent.name)
        if job and job["status"] == "done":
            r = job["result"]
            found.append({"id": job["id"], "made": job["created"], "redo": job.get("redo"),
                          **{k: r.get(k) for k in ("title", "key", "meter", "kind", "bars")}})
    found.sort(key=lambda j: j["made"], reverse=True)
    return found[:limit]


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _job_from_disk(job_id: str) -> dict | None:
    """A job the server no longer has in memory (it was restarted): rebuild it from its folder."""
    folder = JOBS / job_id
    if not JOB_ID.match(job_id) or not folder.is_dir():
        return None
    info = _read_json(folder / JOB_FILE) or {}
    result = _read_json(folder / "result.json")
    base = {"id": job_id, "title": info.get("title"), "source": info.get("source"), "redo": info.get("redo"),
            "created": info.get("created") or folder.stat().st_mtime}
    if result and (folder / "score.pdf").exists():
        result.pop("polyphony", None)
        if not info.get("created"):
            base["created"] = (folder / "result.json").stat().st_mtime
        return {**base, "status": "done", "stage": "Done", "progress": 1.0, "result": result}
    return {**base, "status": "failed", "stage": "Stopped",
            "error": "This song didn't finish, perhaps because the app was closed. Try it again."}


def _check(job_id: str) -> str:
    if not JOB_ID.match(job_id):
        raise HTTPException(404)
    return job_id


def _safe_name(title: str) -> str:
    return "".join(ch for ch in title if ch.isalnum() or ch in " -_").strip()[:80] or "score"


def _run_job(jobs: Jobs, job_id: str, source: str, workdir: Path, mode: str, meter: str,
             title: str | None, upload_name: str | None, lyrics: str = "") -> None:
    from .pipeline import run

    def progress(stage: str, frac: float) -> None:
        jobs.update(job_id, status="working", stage=stage, progress=round(frac, 3))

    try:
        result = run(source, workdir, mode=mode, meter=meter, title=title, upload_name=upload_name, progress=progress,
                     lyrics=lyrics)
        jobs.update(job_id, status="done", stage="Done", progress=1.0, result=result.to_dict())
    except Exception as e:  # report any failure on the page in plain words
        jobs.update(job_id, status="failed", stage="Stopped", error=str(e) or e.__class__.__name__)
        shutil.rmtree(workdir, ignore_errors=True)  # nothing worth keeping, and it shouldn't be listed
    finally:
        # Keep only the score files; the audio copies are large.
        for pattern in ("upload.bin", "source.*", "vocals.wav", "mix.wav"):
            for leftover in workdir.glob(pattern):
                leftover.unlink(missing_ok=True)


def already_running(url: str) -> bool:
    """True when a copy of choir-solfa is already answering at this address."""
    try:
        with urllib.request.urlopen(f"{url}/api/ping", timeout=2) as r:
            return json.load(r).get("app") == "choir-solfa"
    except (OSError, ValueError):
        return False


def serve(port: int = 4790, open_browser: bool = True) -> int:
    import uvicorn

    url = f"http://127.0.0.1:{port}"
    if already_running(url):  # started twice: show the page that is already there
        print(f"choir-solfa is already running at {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return 0
    JOBS.mkdir(parents=True, exist_ok=True)
    app = create_app(port)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} choir-solfa is running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
