"""Make one song on AWS Fargate, then exit (the task stops and costs nothing more).

The website's API (cloud/api/handler.py) saves the request in DynamoDB and starts a
Fargate task running `python -m choir_solfa.cloud_worker` with JOB_ID set. This reads the
request, gets the song (link, or an upload in S3), runs the usual pipeline while writing
progress to DynamoDB for the page to show, and puts the PDF, text and practice tracks in S3.

Environment: JOB_ID, TABLE, BUCKET, and optionally MAX_SONG_MINUTES and MAX_TASK_MINUTES.

YouTube refuses downloads from AWS addresses, so for links the task first opens a free
Cloudflare WARP tunnel: wgcf registers a new WARP device and writes its WireGuard profile, and
wireproxy runs that tunnel in user space (Fargate has no VPN device) as an HTTP proxy that
fetch.py uses through CHOIR_SOLFA_PROXY.
"""
from __future__ import annotations

import ipaddress
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

FILES = {"score.pdf": "application/pdf", "score.txt": "text/plain; charset=utf-8"}


HEARTBEAT_SECONDS = 60  # the API counts a song as stopped after a few minutes without one (handler.STALE_SECONDS)
WARP_ENDPOINT = "162.159.192.1:2408"  # engage.cloudflareclient.com; Fargate tasks have IPv4 only
WARP_TRIES = 12  # YouTube refuses some WARP addresses; each new WARP device gets another one
PROXY = "127.0.0.1:1080"  # an HTTP proxy: yt-dlp, urllib and ffmpeg all use it (ffmpeg ignores SOCKS ones)
HEALTH = "127.0.0.1:9080"
NO_TUNNEL = ("Links can't be downloaded right now. Please try again in a few minutes, "
             "or save the song and use Choose an audio file instead.")
SOMETHING_WRONG = "Something went wrong while making this song. Please try again, or try a different recording."
NOT_PUBLIC = "That link doesn't point to a public website. Paste a link to a song on YouTube, Spotify or another music site."


def wireproxy_config(profile: str) -> str:
    """wireproxy's config from a wgcf WireGuard profile: an HTTP proxy, and a health check."""
    lines = []
    for line in profile.splitlines():
        if line.split("=")[0].strip().lower() == "endpoint":
            line = f"Endpoint = {WARP_ENDPOINT}"
        lines.append(line)
        if line.strip() == "[Interface]":
            lines.append("CheckAlive = 1.1.1.1")
    return "\n".join(lines + ["", "[http]", f"BindAddress = {PROXY}", ""])


def start_warp(folder: Path, wait: float = 30) -> subprocess.Popen | None:
    """Open the WARP tunnel and point fetch.py at it. Returns the proxy process, or None."""
    os.environ.pop("CHOIR_SOLFA_PROXY", None)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        for step in (["wgcf", "register", "--accept-tos"], ["wgcf", "generate"]):
            subprocess.run(step, cwd=folder, check=True, capture_output=True, timeout=60)
        config = folder / "wireproxy.conf"
        config.write_text(wireproxy_config((folder / "wgcf-profile.conf").read_text()))
        proxy = subprocess.Popen(["wireproxy", "-s", "-c", str(config), "-i", HEALTH])
    except (OSError, subprocess.SubprocessError) as e:
        print(f"no WARP tunnel: {e!r}", flush=True)
        return None
    deadline = time.time() + wait
    while time.time() < deadline and proxy.poll() is None:
        try:
            with urllib.request.urlopen(f"http://{HEALTH}/readyz", timeout=3) as r:
                if r.status == 200:
                    os.environ["CHOIR_SOLFA_PROXY"] = f"http://{PROXY}"
                    print("WARP tunnel ready", flush=True)
                    return proxy
        except OSError:
            pass
        time.sleep(1)
    print("WARP tunnel did not come up", flush=True)
    proxy.terminate()
    return None


class TookTooLong(Exception):
    pass


def public_address(url: str) -> bool:
    """False when the link's website name leads to a private or internal address (such as
    127.0.0.1, 10.x or 169.254.169.254). A second guard behind the tunnel: the API already
    refuses IP addresses and local names, but a public-looking name can still point inside.
    Spotify track ids have no website name, and a name that doesn't look up is left to the
    download to report."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    if not host:
        return True
    try:
        found = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return True
    for *_, sockaddr in found:
        address = ipaddress.ip_address(sockaddr[0].split("%")[0])
        if not address.is_global or address.is_multicast:
            return False
    return True


def song_key(job_id: str, name: str) -> str:
    return f"songs/{job_id}/{name}"


def update(table, job_id: str, *, while_active: bool = False, **fields) -> None:
    """Set fields on the song's record. while_active: only if it is still waiting or working, so
    a progress report can't bring back a song the API has already marked as stopped."""
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    args = {}
    if while_active:
        names["#status_now"] = "status"
        values.update({":waiting": "waiting", ":working": "working"})
        args["ConditionExpression"] = "#status_now IN (:waiting, :working)"
    try:
        table.update_item(Key={"id": job_id}, UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
                          ExpressionAttributeNames=names, ExpressionAttributeValues=values, **args)
    except Exception as e:
        if while_active and "ConditionalCheckFailed" in type(e).__name__ + str(getattr(e, "response", "")):
            return
        raise


class Heartbeat:
    """Notes on the record every HEARTBEAT_SECONDS that the song is still being made, whatever
    the stage. Some stages (separating the voices with demucs 4.0.1, finding the notes) report
    no progress for many minutes, and without this the API would think the machine had stopped."""
    def __init__(self, table, job_id: str):
        self.table, self.job_id, self.done = table, job_id, threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self) -> None:
        while not self.done.wait(HEARTBEAT_SECONDS):
            try:
                update(self.table, self.job_id, while_active=True, heartbeat=int(time.time()))
            except Exception as e:  # a missed beat is not worth stopping the song for
                print(f"heartbeat failed: {e!r}", flush=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.done.set()


def make_song(job: dict, table, s3, bucket: str, max_seconds: float) -> None:
    from . import pipeline
    from .audio import AudioError
    from .fetch import REFUSED, FetchError

    job_id = job["id"]
    last = [0.0]

    def progress(stage: str, frac: float) -> None:
        now = time.time()
        if now - last[0] < 2 and frac < 1:  # the page checks every 2 seconds
            return
        last[0] = now
        update(table, job_id, while_active=True, status="working", stage=stage, progress=int(frac * 1000),
               heartbeat=int(now))

    progress("Getting the song", 0.01)
    with Heartbeat(table, job_id), tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "job"
        proxy = None
        try:
            warp = False
            if job.get("upload_key"):
                source = Path(tmp) / "upload.bin"
                s3.download_file(bucket, job["upload_key"], str(source))
                source = str(source)
            else:
                source = job["url"]
                warp = bool(os.environ.get("USE_WARP"))
                if not public_address(source):
                    raise FetchError(NOT_PUBLIC)
            tries = WARP_TRIES if warp else 1
            for attempt in range(1, tries + 1):
                if warp:
                    # Links only ever go through the tunnel, never straight from AWS, so a link
                    # can't reach anything inside AWS (such as the task's own metadata address).
                    proxy = start_warp(Path(tmp) / f"warp{attempt}")
                    if proxy is None:
                        if attempt == tries:
                            raise FetchError(NO_TUNNEL)
                        continue
                try:
                    result = pipeline.run(source, work, mode=job.get("mode", "auto"), meter=job.get("meter", "auto"),
                                          title=job.get("title_asked") or None, upload_name=job.get("upload_name"),
                                          progress=progress, max_seconds=max_seconds, lyrics=job.get("lyrics"))
                    break
                except FetchError as e:
                    if str(e) != REFUSED or attempt == tries:
                        raise
                    print(f"YouTube refused WARP address {attempt}; trying another", flush=True)
                finally:
                    if proxy:
                        proxy.terminate()
                        proxy.wait()
                        proxy = None
            uploads = dict(FILES) | {name: "audio/mp4" for name in result.tracks.values()}
            for name, content_type in uploads.items():
                s3.upload_file(str(work / name), bucket, song_key(job_id, name), ExtraArgs={"ContentType": content_type})
            update(table, job_id, status="done", stage="Done", progress=1000, title=result.title,
                   result=json.dumps(result.to_dict()), finished=int(time.time()))
        except Exception as e:  # report any failure on the page in plain words
            # Only our own messages, which are written for choir leaders, reach the page; anything
            # else (paths, bucket names, library errors) stays in the task's log.
            message = str(e) if isinstance(e, (FetchError, AudioError)) and str(e) else SOMETHING_WRONG
            if isinstance(e, TookTooLong):
                message = "This song took far too long to make, so it was stopped. Try a shorter recording."
            print(f"failed: {e!r}\n{traceback.format_exc()}", flush=True)
            update(table, job_id, status="failed", stage="Stopped", error=message[:500], finished=int(time.time()))
        finally:
            if job.get("upload_key"):  # the upload has done its job; only the score is kept
                s3.delete_object(Bucket=bucket, Key=job["upload_key"])


def main() -> int:
    import boto3

    job_id = os.environ["JOB_ID"]
    table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
    s3 = boto3.client("s3")
    job = table.get_item(Key={"id": job_id}).get("Item")
    if not job:
        print(f"no such job {job_id}", flush=True)
        return 1
    if job.get("status") not in ("waiting", "working"):
        print(f"job {job_id} is already {job.get('status')}", flush=True)
        return 0

    # A safety stop, so a stuck song can't keep a machine (and the bill) running.
    def stop(signum, frame):
        raise TookTooLong()
    signal.signal(signal.SIGALRM, stop)
    signal.alarm(int(float(os.environ.get("MAX_TASK_MINUTES", "60")) * 60))

    max_seconds = float(os.environ.get("MAX_SONG_MINUTES", "10")) * 60
    print(f"making {job_id}", flush=True)
    started = time.time()
    make_song(job, table, s3, os.environ["BUCKET"], max_seconds)
    print(f"finished {job_id} in {time.time() - started:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
