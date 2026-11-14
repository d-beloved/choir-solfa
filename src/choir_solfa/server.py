"""Local web page: paste a link or drop a file, watch progress, download the score.

Bound to 127.0.0.1 only, with a Host check (stops DNS-rebinding pages from
reaching it) and a per-run token on every write request.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

HOME = Path(os.environ.get("CHOIR_SOLFA_HOME", Path.home() / ".choir-solfa"))
JOBS = HOME / "jobs"
WEB = Path(__file__).parent / "web"
MAX_UPLOAD = 300 * 1024 * 1024
JOB_ID = re.compile(r"^[0-9a-f]{32}$")


class Jobs:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict[str, dict] = {}
        self.pool = ThreadPoolExecutor(max_workers=1)  # one song at a time; the rest wait in line

    def add(self, **info) -> str:
        job_id = uuid.uuid4().hex
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
            return {k: v for k, v in job.items() if not k.startswith("_")} if job else None


def create_app(port: int) -> FastAPI:
    token = secrets.token_urlsafe(24)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    jobs = Jobs()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def host_check(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "Wrong host."}, status_code=403)
        if request.method != "GET" and not secrets.compare_digest(request.headers.get("x-token", ""), token):
            return JSONResponse({"error": "Missing or wrong token. Reload the page."}, status_code=403)
        return await call_next(request)

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
        workdir = JOBS / uuid.uuid4().hex
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

        job_id = jobs.add(title=title or upload_name or source, source=source if is_url(source) else upload_name)
        jobs.update(job_id, _dir=str(workdir))
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
        with jobs.lock:
            job = jobs.state.get(_check(job_id))
            workdir = job and job.get("_dir")
        path = Path(workdir) / name if workdir else None
        if not path or not path.exists():
            raise HTTPException(404, "Not ready.")
        stem = _safe_name(job.get("result", {}).get("title") or "score")
        filename = f"{stem}{Path(name).suffix}"
        disposition = "attachment" if download else "inline"
        return FileResponse(path, media_type=files[name],
                            headers={"Content-Disposition": f'{disposition}; filename="{filename}"'})

    return app


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
    finally:
        # Keep only the score files; the audio copies are large.
        for pattern in ("upload.bin", "source.*", "vocals.wav", "mix.wav"):
            for leftover in workdir.glob(pattern):
                leftover.unlink(missing_ok=True)


def serve(port: int = 4790, open_browser: bool = True) -> int:
    import uvicorn

    JOBS.mkdir(parents=True, exist_ok=True)
    app = create_app(port)
    url = f"http://127.0.0.1:{port}"
    print(f"choir-solfa is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
