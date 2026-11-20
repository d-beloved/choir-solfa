"""The website's API on AWS Lambda (behind API Gateway, which checks the Cognito sign-in).

Apart from /api/auth/* (signing in, below), every request comes from a signed-in person.
API Gateway has already checked their ID token and passes its claims along: `sub` (their
user id), `email` and `cognito:groups` (admins are in the "admins" group).

Songs are made by a Fargate task per song (src/choir_solfa/cloud_worker.py). Records live
in DynamoDB for good; the files in S3 are deleted after KEEP_DAYS by a bucket rule.

Uses only the Python standard library and boto3, which Lambda provides.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlencode, urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr, Key

TABLE = boto3.resource("dynamodb").Table(os.environ.get("TABLE", "choir-solfa-test"))
REGION = os.environ.get("AWS_REGION", "us-east-2")
# The regional address, so uploads from the page aren't redirected (which breaks them).
S3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                  config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
ECS = boto3.client("ecs")
BUCKET = os.environ.get("BUCKET", "")
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "30"))
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "5"))
MAX_RUNNING = int(os.environ.get("MAX_RUNNING", "5"))
MAX_RUNNING_EACH = int(os.environ.get("MAX_RUNNING_EACH", "2"))  # songs one person can have being made at once
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
MAX_SONG_MINUTES = int(os.environ.get("MAX_SONG_MINUTES", "10"))
ADMIN_GROUP = "admins"
STALE_SECONDS = 5 * 60  # no heartbeat from a song's machine for this long: it stopped
START_SECONDS = 15 * 60  # how long a machine may take to start (fetching its image) before it counts as stopped
FREE_FAILURE_SECONDS = 3 * 60  # a song that fails sooner than this doesn't count toward the daily limit
DAY = 24 * 3600
ADMIN_ROWS = 2000  # newest songs shown on the admin list
UPLOAD_KEY = re.compile(r"^uploads/(?P<sub>[0-9a-zA-Z-]{1,64})/[0-9a-f]{32}$")
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
METERS = ("4/4", "3/4", "2/4", "6/8")
# Not a song: a record whose version number makes new songs take turns, so two requests at the
# same moment can't both slip under the limits (see _start_record).
GATE = "_gate"
BUSY = "Lots of songs are being made right now. Please try again in a few minutes."
FILES = {"score.pdf": "application/pdf", "score.mid": "audio/midi", "score.txt": "text/plain; charset=utf-8"}
AUTH_DOMAIN = os.environ.get("AUTH_DOMAIN", "")  # the sign-in service, e.g. https://auth.solfa.example.com
CLIENT_ID = os.environ.get("CLIENT_ID", "")
# The 30-day renewal token lives in this cookie: the page's scripts can't read it, and the browser
# sends it only to /api/auth on this site.
RENEW_COOKIE = "__Secure-solfa-renew"
RENEW_DAYS = 30
SIGN_IN_AGAIN = "Please sign in again."


class Problem(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def handler(event, context):
    try:
        method = event["requestContext"]["http"]["method"]
        path = event.get("rawPath", "")
        body = _body(event)
        if path.startswith("/api/auth/"):  # no sign-in yet: API Gateway lets these through
            headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
            return sign_in_route(method, path, body, event.get("cookies") or [], headers.get("content-type", ""))
        user = _user(event)
        return _reply(200, route(method, path, body, user))
    except Problem as p:
        return _reply(p.status, {"detail": str(p)})


def route(method: str, path: str, body: dict, user: dict):
    parts = [p for p in path.split("/") if p][1:]  # drop the leading "api"
    if method == "GET" and parts == ["me"]:
        return me(user)
    if method == "POST" and parts == ["uploads"]:
        return new_upload(user, body)
    if method == "POST" and parts == ["jobs"]:
        return create_job(user, body)
    if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
        return job_status(user, parts[1])
    if method == "GET" and parts == ["songs"]:
        return songs(user)
    if method == "DELETE" and len(parts) == 2 and parts[0] == "songs":
        return remove_song(user, parts[1])
    if method == "GET" and parts == ["admin", "songs"]:
        return admin_songs(user)
    raise Problem(404, "Not found.")


# ---------- signing in ----------
# The page sends people to Cognito's sign-in page with a one-time code (PKCE) and gets a code
# back. These routes swap it for tokens with Cognito, so the long-lived renewal token never
# reaches the page: it goes into an HttpOnly cookie. The page keeps only the hour-long ID token,
# in memory, and asks /api/auth/renew for a new one when it runs out or the page is reopened.

def sign_in_route(method: str, path: str, body: dict, cookies: list[str], content_type: str) -> dict:
    action = path[len("/api/auth/"):]
    if method != "POST":
        raise Problem(404, "Not found.")
    # A JSON body can't be sent from another website without the browser asking this site
    # first (and it says no), so other sites can't sign someone in or out.
    if content_type.split(";")[0].strip().lower() != "application/json":
        raise Problem(415, "Bad request.")
    if action == "token":
        fields = {k: body.get(k) for k in ("code", "code_verifier", "redirect_uri")}
        if not all(isinstance(v, str) and 0 < len(v) <= 2000 for v in fields.values()):
            raise Problem(400, "Signing in didn't finish. Please try again.")
        tokens = _cognito_token({"grant_type": "authorization_code", **fields})
        if not tokens or not tokens.get("refresh_token"):
            raise Problem(400, "Signing in didn't finish. Please try again.")
        return _reply(200, {"id": tokens["id_token"]}, cookies=[_renew_cookie(tokens["refresh_token"])])
    renew = _cookie(cookies, RENEW_COOKIE)
    if action == "renew":
        tokens = _cognito_token({"grant_type": "refresh_token", "refresh_token": renew}) if renew else None
        if not tokens:
            return _reply(401, {"detail": SIGN_IN_AGAIN}, cookies=[_renew_cookie("", 0)] if renew else None)
        # Cognito only sends a new renewal token if it is set to rotate them.
        newer = tokens.get("refresh_token")
        return _reply(200, {"id": tokens["id_token"]}, cookies=[_renew_cookie(newer)] if newer else None)
    if action == "signout":
        if renew:  # so the token can't be used again even if it was copied
            try:
                _cognito("oauth2/revoke", {"token": renew})
            except (urllib.error.URLError, OSError):
                pass
        return _reply(200, {}, cookies=[_renew_cookie("", 0)])
    raise Problem(404, "Not found.")


def _cognito_token(fields: dict) -> dict | None:
    """Tokens from Cognito, or None if it refuses (a used code, an expired renewal token)."""
    try:
        return json.loads(_cognito("oauth2/token", fields))
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            return None
        raise Problem(502, "Signing in isn't working right now. Please try again in a minute.")
    except (urllib.error.URLError, OSError, ValueError):
        raise Problem(502, "Signing in isn't working right now. Please try again in a minute.")


def _cognito(page: str, fields: dict) -> bytes:
    request = urllib.request.Request(
        f"{AUTH_DOMAIN}/{page}", data=urlencode({"client_id": CLIENT_ID, **fields}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as res:
        return res.read()


def _renew_cookie(value: str, max_age: int = RENEW_DAYS * 86400) -> str:
    return f"{RENEW_COOKIE}={value}; Max-Age={max_age}; Path=/api/auth; Secure; HttpOnly; SameSite=Strict"


def _cookie(cookies: list[str], name: str) -> str | None:
    for cookie in cookies:
        key, _, value = cookie.partition("=")
        if key.strip() == name and value.strip():
            return value.strip()
    return None


# ---------- who is asking ----------

def _user(event) -> dict:
    claims = (event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}) or {}).get("claims", {})
    if not claims.get("sub"):
        raise Problem(401, SIGN_IN_AGAIN)
    groups = claims.get("cognito:groups", "")
    if isinstance(groups, str):  # API Gateway passes lists as "[a b]"
        groups = groups.strip("[]").replace(",", " ").split()
    email = claims.get("email", "")
    if email and str(claims.get("email_verified", "")).lower() != "true":
        email += " (not confirmed)"
    return {"sub": claims["sub"], "email": email, "admin": ADMIN_GROUP in groups}


def me(user: dict) -> dict:
    used = 0 if user["admin"] else _usage(user)["mine_today"]
    return {"email": user["email"], "admin": user["admin"], "daily_limit": None if user["admin"] else DAILY_LIMIT,
            "left_today": None if user["admin"] else max(0, DAILY_LIMIT - used), "keep_days": KEEP_DAYS,
            "max_minutes": MAX_SONG_MINUTES, "max_upload_mb": MAX_UPLOAD_MB}


# ---------- making a song ----------

def new_upload(user: dict, body: dict) -> dict:
    """A one-off address the page sends the audio file to, straight into S3."""
    name = str(body.get("name") or "song")[:200]
    try:
        size = int(body.get("size") or 0)
    except (TypeError, ValueError):
        raise Problem(400, "Bad request.")
    if size < 1:
        raise Problem(400, "That file is empty. Please choose another one.")
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raise Problem(413, f"That file is larger than {MAX_UPLOAD_MB} MB.")
    key = f"uploads/{user['sub']}/{uuid.uuid4().hex}"
    post = S3.generate_presigned_post(BUCKET, key, ExpiresIn=3600,
                                      Conditions=[["content-length-range", 1, MAX_UPLOAD_MB * 1024 * 1024]])
    return {"url": post["url"], "fields": post["fields"], "key": key, "name": name}


def create_job(user: dict, body: dict) -> dict:
    mode, meter = body.get("mode", "auto"), body.get("meter", "auto")
    title = str(body.get("title") or "").strip()[:120]
    url = str(body.get("url") or "").strip()
    upload_key = str(body.get("upload_key") or "")
    upload_name = str(body.get("upload_name") or "")[:200]
    if mode not in ("auto", "choir", "solo") or (meter != "auto" and meter not in METERS):
        raise Problem(400, "Unknown option.")
    if upload_key:
        m = UPLOAD_KEY.match(upload_key)
        if not m or m.group("sub") != user["sub"]:
            raise Problem(400, "Choose the file again.")
        url = ""
    elif not public_link(url):
        raise Problem(400, "Paste a link to a song or choose a file.")

    job_id = uuid.uuid4().hex
    now = int(time.time())
    item = {"id": job_id, "user": user["sub"], "email": user["email"], "created": now, "status": "waiting",
            "stage": "Starting a machine for your song", "progress": 0, "heartbeat": now, "mode": mode, "meter": meter,
            "title_asked": title, "title": title or upload_name or url, "source": upload_name or url,
            "files_until": now + KEEP_DAYS * 86400}
    if upload_key:
        item.update(upload_key=upload_key, upload_name=upload_name)
    else:
        item["url"] = url
    _start_record(user, item)
    try:
        started = ECS.run_task(
            cluster=os.environ["CLUSTER"], taskDefinition=os.environ["TASK_DEFINITION"], launchType="FARGATE",
            count=1, startedBy=f"song-{job_id[:20]}",
            networkConfiguration={"awsvpcConfiguration": {
                "subnets": os.environ["SUBNETS"].split(","), "securityGroups": [os.environ["SECURITY_GROUP"]],
                "assignPublicIp": "ENABLED"}},
            overrides={"containerOverrides": [{"name": os.environ.get("CONTAINER", "worker"),
                                               "environment": [{"name": "JOB_ID", "value": job_id}]}]},
        )
        if started.get("failures") or not started.get("tasks"):
            raise RuntimeError(started.get("failures"))
    except Exception as e:
        print(f"run_task failed for {job_id}: {e!r}")
        _set(job_id, status="failed", stage="Stopped", error="Couldn't start making this song. Please try again.",
             finished=int(time.time()))
        raise Problem(503, "Couldn't start making this song. Please try again in a minute.")
    _set(job_id, task=started["tasks"][0].get("taskArn", ""))  # to find the machine's log if it goes wrong
    return {"id": job_id}


def public_link(url: str) -> bool:
    """A link to a song on a public website: never an IP address or a local or internal name.

    The song machine fetches it (through a Cloudflare WARP tunnel, so AWS's own addresses are out
    of reach anyway); this keeps obviously internal targets out from the start.
    """
    if re.fullmatch(r"spotify:track:[A-Za-z0-9]{22}", url):
        return True
    if len(url) > 2000 or not re.fullmatch(r"https?://\S+", url):
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        parsed.port  # noqa: B018 - raises on a bad port
    except ValueError:
        return False
    labels = host.split(".")
    if len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", label) for label in labels):
        return False  # also refuses IPv6 addresses, which have colons
    if not re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]+", labels[-1]):
        return False  # an IP address in any spelling (10.0.0.1, 0xa.0.0.1, 167772161) ends in digits
    return labels[-1] not in ("localhost", "local", "internal", "localdomain", "home", "lan", "corp", "intranet")


def _start_record(user: dict, item: dict) -> None:
    """Check the limits and save the new song's record in one step that can't be raced.

    The counts come from strongly consistent reads of two small records (see _usage), and the
    song is written together with new versions of them, on condition their versions haven't
    moved. If another song started in between, the write is refused and this counts again.
    """
    for _ in range(6):
        use = _usage(user)
        if not user["admin"] and use["mine_today"] >= DAILY_LIMIT:
            raise Problem(429, f"You've made {DAILY_LIMIT} songs in the last 24 hours, which is the most for now. "
                               "Please come back tomorrow.")
        if not user["admin"] and use["mine_running"] >= MAX_RUNNING_EACH:
            raise Problem(429, f"You already have {use['mine_running']} songs being made. Please wait for one to finish.")
        if len(use["active"]) >= MAX_RUNNING:
            raise Problem(503, BUSY)
        writes = [
            {"Put": {"TableName": TABLE.name, "Item": item, "ConditionExpression": "attribute_not_exists(id)"}},
            {"Update": {"TableName": TABLE.name, "Key": {"id": GATE}, "UpdateExpression": "SET v = :next, active = :active",
                        "ConditionExpression": "attribute_not_exists(v) OR v = :seen",
                        "ExpressionAttributeValues": {":next": use["v"] + 1, ":seen": use["v"],
                                                      ":active": {**use["active"], item["id"]: user["sub"]}}}},
        ]
        if not user["admin"]:
            writes.append({"Put": {
                "TableName": TABLE.name,
                "Item": {"id": _person_key(user), "v": use["person_v"] + 1,
                         "recent": use["recent"] + [{"id": item["id"], "created": item["created"]}]},
                "ConditionExpression": "attribute_not_exists(v) OR v = :seen",
                "ExpressionAttributeValues": {":seen": use["person_v"]}}})
        try:
            TABLE.meta.client.transact_write_items(TransactItems=writes)
            return
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            time.sleep(0.05)
    raise Problem(503, BUSY)


def _person_key(user: dict) -> str:
    return f"_person#{user['sub']}"


def _usage(user: dict) -> dict:
    """What counts toward the limits, read without scanning the table.

    The GATE record lists the songs that were being made when the last song started, and each
    person's own record lists their songs from the last 24 hours. Those songs' records are then
    read (all strongly consistent) to see which are still being made and which count today.
    Neither list is a song, and neither has the "user" field, so they aren't in the by-user index.
    """
    now = int(time.time())
    gate = TABLE.get_item(Key={"id": GATE}, ConsistentRead=True).get("Item") or {}
    active = {k: v for k, v in (gate.get("active") or {}).items()}
    person = {} if user["admin"] else TABLE.get_item(Key={"id": _person_key(user)}, ConsistentRead=True).get("Item")
    if person is None:  # first song since the person record was added: start it from the index
        res = TABLE.query(IndexName="by-user", KeyConditionExpression=Key("user").eq(user["sub"]) &
                          Key("created").gte(now - DAY))
        person = {"recent": [{"id": i["id"], "created": i["created"]} for i in res.get("Items", [])], "v": None}
    recent = [r for r in person.get("recent") or [] if int(r["created"]) >= now - DAY]
    songs = _read_songs(set(active) | {r["id"] for r in recent})
    active = {k: v for k, v in active.items() if k in songs and _being_made(songs[k], now)}
    return {
        "v": int(gate.get("v", 0)), "active": active,
        "person_v": int(person["v"]) if person.get("v") is not None else 0,
        "recent": [{"id": r["id"], "created": int(r["created"])} for r in recent],
        "mine_running": sum(1 for owner in active.values() if owner == user["sub"]),
        "mine_today": sum(1 for r in recent if r["id"] not in songs or _counts_today(songs[r["id"]])),
    }


def _read_songs(ids: set[str]) -> dict[str, dict]:
    found, keys = {}, [{"id": i} for i in ids]
    while keys:
        batch, keys = keys[:100], keys[100:]
        request = {TABLE.name: {"Keys": batch, "ConsistentRead": True,
                                "ProjectionExpression": "id, #s, created, heartbeat, finished",
                                "ExpressionAttributeNames": {"#s": "status"}}}
        while request:
            res = TABLE.meta.client.batch_get_item(RequestItems=request)
            for item in res.get("Responses", {}).get(TABLE.name, []):
                found[item["id"]] = item
            request = res.get("UnprocessedKeys") or None
    return found


def _being_made(item: dict, now: float) -> bool:
    """Still being made: its machine has been heard from lately. The worker notes this every
    minute (cloud_worker.Heartbeat); a machine that is starting gets longer, to fetch its image."""
    if item.get("status") not in ("waiting", "working"):
        return False
    quiet = now - int(item.get("heartbeat") or item["created"])
    return quiet < (START_SECONDS if item["status"] == "waiting" else STALE_SECONDS)


def _counts_today(item: dict) -> bool:
    """Every song counts toward the daily limit, except one that failed within a few minutes
    (a bad link, or a machine that wouldn't start). A song that ran a long time and then failed
    still used a machine, so it counts."""
    if item.get("status") != "failed":
        return True
    finished = item.get("finished")
    return finished is None or int(finished) - int(item["created"]) >= FREE_FAILURE_SECONDS


# ---------- following and opening songs ----------

def _get(user: dict, job_id: str) -> dict:
    if not JOB_ID.match(job_id):
        raise Problem(404, "No such song.")
    item = TABLE.get_item(Key={"id": job_id}).get("Item")
    if not item or item.get("removed") or (item["user"] != user["sub"] and not user["admin"]):
        raise Problem(404, "No such song.")
    if item["status"] in ("waiting", "working") and not _being_made(item, time.time()):
        error = "This song stopped part way. Please try it again."
        try:  # only if the machine hasn't been heard from since this was read
            TABLE.update_item(
                Key={"id": job_id}, UpdateExpression="SET #s = :failed, stage = :stage, #e = :error, finished = :now",
                ConditionExpression="#s = :seen AND (attribute_not_exists(heartbeat) OR heartbeat = :beat)",
                ExpressionAttributeNames={"#s": "status", "#e": "error"},
                ExpressionAttributeValues={":failed": "failed", ":stage": "Stopped", ":error": error,
                                           ":now": int(time.time()), ":seen": item["status"],
                                           ":beat": item.get("heartbeat", 0)})
            item.update(status="failed", stage="Stopped", error=error)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            item = TABLE.get_item(Key={"id": job_id}, ConsistentRead=True)["Item"]  # it spoke up just now
    return item


def job_status(user: dict, job_id: str) -> dict:
    item = _get(user, job_id)
    job = {"id": job_id, "status": item["status"], "stage": item.get("stage"), "progress": int(item.get("progress") or 0) / 1000,
           "created": int(item["created"]), "title": item.get("title"), "source": item.get("source"),
           "redo": _redo(item), "error": item.get("error")}
    if item["status"] == "done":
        job["result"] = json.loads(item["result"])
        job["expired"] = _expired(item)
        if not job["expired"]:
            job["files"] = _file_links(job_id, job["result"].get("title") or "score")
    return job


def songs(user: dict) -> list[dict]:
    items, start = [], None
    while True:
        args = {"IndexName": "by-user", "KeyConditionExpression": Key("user").eq(user["sub"]), "ScanIndexForward": False}
        if start:
            args["ExclusiveStartKey"] = start
        res = TABLE.query(**args)
        items += res.get("Items", [])
        start = res.get("LastEvaluatedKey")
        if not start or len(items) >= 300:
            break
    now = time.time()
    found = []
    for item in items:
        busy = _being_made(item, now)
        if item.get("removed") or not (item["status"] == "done" or busy):
            continue
        entry = {"id": item["id"], "made": int(item["created"]), "redo": _redo(item), "status": item["status"],
                 "title": item.get("title")}
        if item["status"] == "done":
            r = json.loads(item["result"])
            entry.update({k: r.get(k) for k in ("title", "key", "meter", "kind", "bars")}, expired=_expired(item))
        found.append(entry)
    return found


def remove_song(user: dict, job_id: str) -> dict:
    item = _get(user, job_id)
    if item["status"] in ("waiting", "working"):
        raise Problem(409, "This song is still being made.")
    S3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": f"songs/{job_id}/{n}"} for n in FILES]})
    _set(job_id, removed=True, removed_at=int(time.time()))  # the record stays; the files go
    return {"removed": job_id}


def admin_songs(user: dict) -> list[dict]:
    if not user["admin"]:
        raise Problem(403, "Only admins can see this.")
    items, start = [], None
    while True:
        args = {"ProjectionExpression": "id, email, #t, #s, created, #e, #r, removed, #src",
                "FilterExpression": Attr("created").exists(),  # songs only, not the GATE record
                "ExpressionAttributeNames": {"#t": "title", "#s": "status", "#e": "error", "#r": "result", "#src": "source"}}
        if start:
            args["ExclusiveStartKey"] = start
        res = TABLE.scan(**args)
        items += res.get("Items", [])
        start = res.get("LastEvaluatedKey")
        if not start:
            break
    rows = []
    for item in items:
        r = json.loads(item["result"]) if item.get("result") else {}
        rows.append({"id": item["id"], "email": item.get("email"), "title": item.get("title"), "status": item["status"],
                     "made": int(item["created"]), "error": item.get("error"), "source": item.get("source"),
                     "key": r.get("key"), "kind": r.get("kind"), "removed": bool(item.get("removed"))})
    rows.sort(key=lambda r: r["made"], reverse=True)
    return rows[:ADMIN_ROWS]  # a reply must stay under Lambda's 6 MB


def _redo(item: dict) -> dict | None:
    if not item.get("url"):
        return None  # an upload's audio isn't kept, so it can't be made again
    return {"url": item["url"], "mode": item.get("mode", "auto"), "meter": item.get("meter", "auto"),
            "title": item.get("title_asked", "")}


def _expired(item: dict) -> bool:
    return time.time() > int(item.get("files_until") or 0)


def _file_links(job_id: str, title: str) -> dict:
    stem = "".join(ch for ch in title if ch.isalnum() or ch in " -_").strip()[:80] or "score"
    links = {}
    for name, content_type in FILES.items():
        ext = name.split(".")[1]
        for download in (False, True):
            params = {"Bucket": BUCKET, "Key": f"songs/{job_id}/{name}", "ResponseContentType": content_type,
                      "ResponseContentDisposition": f'{"attachment" if download else "inline"}; filename="{stem}.{ext}"'}
            links[f"{ext}{'_download' if download else ''}"] = S3.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=6 * 3600)
    return links


# ---------- plumbing ----------

def _set(job_id: str, **fields) -> None:
    TABLE.update_item(Key={"id": job_id}, UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
                      ExpressionAttributeNames={f"#{k}": k for k in fields},
                      ExpressionAttributeValues={f":{k}": v for k, v in fields.items()})


def _body(event) -> dict:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        raise Problem(400, "Bad request.")
    return data if isinstance(data, dict) else {}


def _reply(status: int, data, cookies: list[str] | None = None) -> dict:
    reply = {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store"},
             "body": json.dumps(data, default=_plain)}
    if cookies:
        reply["cookies"] = cookies
    return reply


def _plain(value):
    from decimal import Decimal
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    raise TypeError(type(value))
