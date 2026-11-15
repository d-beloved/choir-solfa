"""Local web page: paste a link or drop a file, watch progress, download the score.

Bound to 127.0.0.1 only, with a Host check (stops DNS-rebinding pages from
reaching it) and a per-run token on every write request. With restart_when_idle
(for running under launchd) the token is kept in HOME/token so it survives the
server's own restarts.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import threading
import time
import urllib.request
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

HOME = Path(os.environ.get("CHOIR_SOLFA_HOME", Path.home() / ".choir-solfa"))
JOBS = HOME / "jobs"
CODE = Path(__file__).parent
WEB = CODE / "web"
MAX_UPLOAD = 300 * 1024 * 1024
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
JOB_FILE = "job.json"  # how the song was asked for: link, settings, time


class Jobs:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict[str, dict] = {}
        self.pool = ThreadPoolExecutor(max_workers=1)  # one song at a time; the rest wait in line
        self.last_finished = 0.0  # when the last song finished or failed; 0 if none yet

    def add(self, job_id: str, **info) -> str:
        with self.lock:
            self.state[job_id] = {"id": job_id, "status": "waiting", "stage": "Waiting to start", "progress": 0.0,
                                  "created": time.time(), **info}
        return job_id

    def update(self, job_id: str, **fields) -> None:
        with self.lock:
            self.state[job_id].update(fields)
            if fields.get("status") in ("done", "failed"):
                self.last_finished = time.time()

    def any_busy(self) -> bool:
        with self.lock:
            return any(job.get("status") in ("waiting", "working") for job in self.state.values())

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.state.get(job_id)
            if job:
                return {k: v for k, v in job.items() if not k.startswith("_")}
        return _job_from_disk(job_id)

    def busy(self, job_id: str) -> bool:
        with self.lock:
            return self.state.get(job_id, {}).get("status") in ("waiting", "working")


def code_stamp() -> float:
    """Newest change to the app's own code and page."""
    return max((p.stat().st_mtime for p in CODE.rglob("*") if p.suffix in (".py", ".html") and p.is_file()),
               default=0.0)


class Restarter:
    """Decides when an idle server should quit so launchd starts a fresh copy.

    A fresh copy picks up code changes and gives back the memory the music models
    keep after a song (about 2 GB). It never quits while a song is waiting or being
    made, nor until the page has been left alone for `quiet` seconds.
    """

    def __init__(self, jobs: Jobs, quiet: float = 120.0, settle: float = 10.0, stamp=code_stamp, clock=time.time):
        self.jobs, self.quiet, self.settle, self.stamp, self.clock = jobs, quiet, settle, stamp, clock
        self.started_stamp = stamp()
        self.last_request = clock()

    def touch(self) -> None:
        self.last_request = self.clock()

    def reason(self) -> str | None:
        if self.jobs.any_busy():
            return None
        now = self.clock()
        if now - max(self.last_request, self.jobs.last_finished) < self.quiet:
            return None
        if self.jobs.last_finished:
            return "a song was made, so its memory can be given back"
        stamp = self.stamp()
        if stamp != self.started_stamp and now - stamp >= self.settle:  # wait for a burst of saves to finish
            return "the code changed"
        return None


class Closer:
    """Decides when the desktop app should quit because nobody is using it.

    An open page calls /api/ping every minute, so this waits until no page has been
    open for `after` seconds. It never quits while a song is waiting or being made.
    """

    def __init__(self, jobs: Jobs, after: float = 15 * 60.0, clock=time.time):
        self.jobs, self.after, self.clock = jobs, after, clock
        self.last_seen = clock()

    def touch(self) -> None:
        self.last_seen = self.clock()

    def reason(self) -> str | None:
        if self.jobs.any_busy() or self.clock() - self.last_seen < self.after:
            return None
        return f"no page has been open for {round(self.after / 60)} minutes"


def _token(persist: bool) -> str:
    if not persist:
        return secrets.token_urlsafe(24)
    path = HOME / "token"
    try:
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(24)
    HOME.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.write_text(token, encoding="utf-8")
    return token


def create_app(port: int, public_hosts: list[str] | None = None, restart_when_idle: bool = False,
               quit_when_closed: bool = False) -> FastAPI:
    """public_hosts: extra names the page may be reached by, e.g. through a Cloudflare Tunnel.
    Put access control (such as Cloudflare Access) in front of any public name: the page hands
    its token to whoever can load it.
    restart_when_idle: set app.state.restarter; serve() then quits when it says so.
    quit_when_closed: set app.state.closer, for the desktop app: quit once no page is open."""
    token = _token(persist=restart_when_idle)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", *(h.strip().lower() for h in public_hosts or [] if h.strip())}
    jobs = Jobs()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    restarter = Restarter(jobs) if restart_when_idle else None
    app.state.restarter = restarter
    closer = Closer(jobs) if quit_when_closed else None
    app.state.closer = closer

    @app.middleware("http")
    async def host_check(request: Request, call_next):
        if restarter and request.url.path != "/api/ping":  # an open page alone shouldn't hold off a restart
            restarter.touch()
        if closer:
            closer.touch()
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
        return (WEB / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", token)

    @app.post("/api/jobs")
    async def create_job(
        url: str = Form(""), mode: str = Form("auto"), meter: str = Form("auto"), title: str = Form(""),
        file: UploadFile | None = File(None),
    ):
        from .fetch import is_url
        from .models import METERS

        if mode not in ("auto", "choir", "solo") or (meter != "auto" and meter not in METERS):
            raise HTTPException(400, "Unknown option.")
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
        redo = {"url": source, "mode": mode, "meter": meter, "title": title} if is_url(source) else None
        info = {"title": title or upload_name or source, "source": source if is_url(source) else upload_name,
                "created": time.time(), "redo": redo}
        (workdir / JOB_FILE).write_text(json.dumps(info, indent=2), encoding="utf-8")
        jobs.add(job_id, **info)
        jobs.pool.submit(_run_job, jobs, job_id, source, workdir, mode, meter, title or None, upload_name)
        return {"id": job_id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = jobs.get(_check(job_id))
        if not job:
            raise HTTPException(404, "No such job.")
        return job

    @app.get("/api/jobs/{job_id}/{name}")
    def job_file(job_id: str, name: str, download: int = 0):
        files = {"score.pdf": "application/pdf", "score.mid": "audio/midi", "score.txt": "text/plain; charset=utf-8"}
        if name not in files:
            raise HTTPException(404)
        job = jobs.get(_check(job_id)) or {}
        path = JOBS / job_id / name
        if not path.exists():
            raise HTTPException(404, "Not ready.")
        stem = _safe_name((job.get("result") or {}).get("title") or "score")
        filename = f"{stem}{Path(name).suffix}"
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
             title: str | None, upload_name: str | None) -> None:
    from .pipeline import run

    def progress(stage: str, frac: float) -> None:
        jobs.update(job_id, status="working", stage=stage, progress=round(frac, 3))

    try:
        result = run(source, workdir, mode=mode, meter=meter, title=title, upload_name=upload_name, progress=progress)
        jobs.update(job_id, status="done", stage="Done", progress=1.0, result=result.to_dict())
    except Exception as e:  # report any failure on the page in plain words
        jobs.update(job_id, status="failed", stage="Stopped", error=str(e) or e.__class__.__name__)
        shutil.rmtree(workdir, ignore_errors=True)  # nothing worth keeping, and it shouldn't be listed
    finally:
        # Keep only the score files; the audio copies are large.
        for pattern in ("upload.bin", "source.*", "vocals.wav", "mix.wav"):
            for leftover in workdir.glob(pattern):
                leftover.unlink(missing_ok=True)


def _watch(watcher: Restarter | Closer, message: str, every: float = 5.0) -> None:
    while True:
        time.sleep(every)
        why = watcher.reason()
        if why:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}: {why}.", flush=True)
            os.kill(os.getpid(), signal.SIGTERM)  # uvicorn shuts down cleanly (launchd then starts a new copy)
            return


def already_running(url: str) -> bool:
    """True when a copy of choir-solfa is already answering at this address."""
    try:
        with urllib.request.urlopen(f"{url}/api/ping", timeout=2) as r:
            return json.load(r).get("app") == "choir-solfa"
    except (OSError, ValueError):
        return False


def serve(port: int = 4790, open_browser: bool = True, public_hosts: list[str] | None = None,
          restart_when_idle: bool = False, quit_when_closed: bool = False) -> int:
    import uvicorn

    url = f"http://127.0.0.1:{port}"
    if already_running(url):  # e.g. the app icon clicked twice: show the page that is already there
        print(f"choir-solfa is already running at {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return 0
    JOBS.mkdir(parents=True, exist_ok=True)
    if public_hosts is None:
        public_hosts = os.environ.get("CHOIR_SOLFA_PUBLIC_HOST", "").split(",")
    public_hosts = [h.strip() for h in public_hosts if h.strip()]
    app = create_app(port, public_hosts, restart_when_idle=restart_when_idle, quit_when_closed=quit_when_closed)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} choir-solfa is running at {url}  (Ctrl+C to stop)", flush=True)
    for host in public_hosts:
        print(f"Also answering at https://{host}", flush=True)
    if app.state.restarter:
        threading.Thread(target=_watch, args=(app.state.restarter, "Quitting to restart fresh"), daemon=True).start()
    if app.state.closer:
        threading.Thread(target=_watch, args=(app.state.closer, "Quitting"), daemon=True).start()
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
