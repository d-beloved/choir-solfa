"""The website's code on AWS (cloud/api/handler.py and choir_solfa.cloud_worker), against moto's
stand-ins for DynamoDB and S3. Starting Fargate tasks is replaced by a stub."""
from __future__ import annotations

import importlib.util
import json
import socket
import time
from pathlib import Path

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

ROOT = Path(__file__).resolve().parent.parent
BUCKET = "choir-solfa-test-data"


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "us-east-2", "AWS_REGION": "us-east-2", "TABLE": "songs-test", "BUCKET": BUCKET,
                 "DAILY_LIMIT": "2", "MAX_RUNNING": "3", "CLUSTER": "c", "TASK_DEFINITION": "t", "SUBNETS": "s1,s2",
                 "SECURITY_GROUP": "sg"}.items():
        monkeypatch.setenv(k, v)
    with moto.mock_aws():
        db = boto3.resource("dynamodb")
        db.create_table(
            TableName="songs-test", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}, {"AttributeName": "user", "AttributeType": "S"},
                                  {"AttributeName": "created", "AttributeType": "N"}],
            GlobalSecondaryIndexes=[{"IndexName": "by-user", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "user", "KeyType": "HASH"},
                                                   {"AttributeName": "created", "KeyType": "RANGE"}]}])
        boto3.client("s3").create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-east-2"})
        yield db.Table("songs-test")


class FakeEcs:
    def __init__(self):
        self.started = []
        self.fail = False

    def run_task(self, **kwargs):
        if self.fail:
            return {"tasks": [], "failures": [{"reason": "no capacity"}]}
        self.started.append(kwargs)
        return {"tasks": [{"taskArn": "arn:task"}], "failures": []}

    def list_tasks(self, **kwargs):
        return {"taskArns": [f"arn:{i}" for i in range(len(self.started))]}


@pytest.fixture
def api(aws):
    spec = importlib.util.spec_from_file_location("cloud_api_handler", ROOT / "cloud" / "api" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ECS = FakeEcs()
    mod._lookup = lambda host: [(2, 1, 6, "", ("93.184.215.14", 0))]  # every name is public unless a test says so
    return mod


def call(api, method, path, user="u1", body=None, admin=False, email="singer@example.com"):
    claims = {"sub": user, "email": email}
    if admin:
        claims["cognito:groups"] = "[admins]"
    event = {"rawPath": path, "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
             "body": json.dumps(body) if body is not None else None}
    res = api.handler(event, None)
    return res["statusCode"], json.loads(res["body"])


def test_no_sign_in_is_refused(api):
    res = api.handler({"rawPath": "/api/me", "requestContext": {"http": {"method": "GET"}}}, None)
    assert res["statusCode"] == 401


def test_me_shows_the_daily_allowance(api):
    status, me = call(api, "GET", "/api/me")
    assert status == 200 and me["left_today"] == 2 and me["admin"] is False and me["keep_days"] == 30
    status, me = call(api, "GET", "/api/me", admin=True)
    assert me["admin"] is True and me["left_today"] is None


def test_making_a_song_starts_one_task_and_keeps_a_record(api, aws):
    status, made = call(api, "POST", "/api/jobs", body={"url": "https://youtu.be/CZmC4qqL_Rg", "meter": "3/4"})
    assert status == 200
    item = aws.get_item(Key={"id": made["id"]})["Item"]
    assert item["user"] == "u1" and item["status"] == "waiting" and item["meter"] == "3/4"
    assert int(item["files_until"]) - int(item["created"]) == 30 * 86400
    (task,) = api.ECS.started
    env = task["overrides"]["containerOverrides"][0]["environment"]
    assert env == [{"name": "JOB_ID", "value": made["id"]}]
    assert task["networkConfiguration"]["awsvpcConfiguration"]["subnets"] == ["s1", "s2"]


def test_bad_requests(api):
    assert call(api, "POST", "/api/jobs", body={"url": "not a link"})[0] == 400
    assert call(api, "POST", "/api/jobs", body={"url": "https://x.y/z", "mode": "loud"})[0] == 400
    # someone else's upload
    assert call(api, "POST", "/api/jobs", body={"upload_key": "uploads/u2/abc"})[0] == 400
    assert call(api, "GET", "/api/nowhere")[0] == 404


def test_daily_limit_but_not_for_admins(api):
    for _ in range(2):
        assert call(api, "POST", "/api/jobs", body={"url": "https://youtu.be/a"})[0] == 200
    status, err = call(api, "POST", "/api/jobs", body={"url": "https://youtu.be/a"})
    assert status == 429 and "tomorrow" in err["detail"]
    assert call(api, "GET", "/api/me")[1]["left_today"] == 0
    api.ECS.started.clear()  # keep under MAX_RUNNING
    assert call(api, "POST", "/api/jobs", user="boss", admin=True, body={"url": "https://youtu.be/a"})[0] == 200


def test_too_many_songs_at_once(api):
    for u in ("a", "b", "c"):
        assert call(api, "POST", "/api/jobs", user=u, body={"url": "https://youtu.be/a"})[0] == 200
    assert call(api, "POST", "/api/jobs", user="d", body={"url": "https://youtu.be/a"})[0] == 503


def test_a_task_that_will_not_start_fails_the_song(api, aws):
    api.ECS.fail = True
    assert call(api, "POST", "/api/jobs", body={"url": "https://youtu.be/a"})[0] == 503
    (item,) = [i for i in aws.scan()["Items"] if not i["id"].startswith("_")]
    assert item["status"] == "failed"


def test_uploads_go_to_the_persons_own_folder(api):
    status, up = call(api, "POST", "/api/uploads", body={"name": "choir.mp3", "size": 1000})
    assert status == 200 and up["key"].startswith("uploads/u1/") and up["fields"]["key"] == up["key"]
    assert up["url"].startswith("https://")
    assert call(api, "POST", "/api/uploads", body={"name": "big.wav", "size": 101 * 1024 * 1024})[0] == 413


def _finished(aws, job_id, user="u1", until=None):
    result = {"title": "Silent Night", "key": "Doh is Bb", "meter": "3/4", "kind": "choir", "bars": 24}
    now = int(time.time())
    aws.put_item(Item={"id": job_id, "user": user, "email": f"{user}@example.com", "created": now - 60, "status": "done",
                       "title": "Silent Night", "result": json.dumps(result), "url": "https://youtu.be/a",
                       "files_until": until if until is not None else now + 86400})


def test_songs_are_private_but_admins_see_them(api, aws):
    _finished(aws, "a" * 32, user="u1")
    status, job = call(api, "GET", f"/api/jobs/{'a' * 32}")
    assert status == 200 and job["status"] == "done" and job["result"]["title"] == "Silent Night"
    assert job["files"]["pdf_download"].startswith("https://") and "attachment" in job["files"]["pdf_download"]
    assert job["redo"]["url"] == "https://youtu.be/a"
    assert call(api, "GET", f"/api/jobs/{'a' * 32}", user="u2")[0] == 404
    assert call(api, "GET", f"/api/jobs/{'a' * 32}", user="boss", admin=True)[0] == 200
    assert call(api, "GET", "/api/admin/songs")[0] == 403
    status, rows = call(api, "GET", "/api/admin/songs", user="boss", admin=True)
    assert [r["email"] for r in rows] == ["u1@example.com"]


def test_expired_songs_keep_their_record_without_files(api, aws):
    _finished(aws, "b" * 32, until=int(time.time()) - 1)
    job = call(api, "GET", f"/api/jobs/{'b' * 32}")[1]
    assert job["expired"] is True and "files" not in job
    (song,) = call(api, "GET", "/api/songs")[1]
    assert song["expired"] is True and song["key"] == "Doh is Bb"


def test_song_list_hides_failed_and_removed(api, aws):
    _finished(aws, "c" * 32)
    now = int(time.time())
    aws.put_item(Item={"id": "d" * 32, "user": "u1", "created": now, "status": "failed", "error": "x"})
    aws.put_item(Item={"id": "e" * 32, "user": "u1", "created": now, "status": "working", "heartbeat": now, "title": "Busy"})
    ids = {s["id"]: s["status"] for s in call(api, "GET", "/api/songs")[1]}
    assert ids == {"c" * 32: "done", "e" * 32: "working"}
    assert call(api, "DELETE", f"/api/songs/{'e' * 32}")[0] == 409  # still being made
    assert call(api, "DELETE", f"/api/songs/{'c' * 32}")[0] == 200
    assert aws.get_item(Key={"id": "c" * 32})["Item"]["removed"] is True  # the record stays
    assert [s["id"] for s in call(api, "GET", "/api/songs")[1]] == ["e" * 32]


def test_a_silent_song_machine_is_marked_stopped(api, aws):
    old = int(time.time()) - 3600
    aws.put_item(Item={"id": "f" * 32, "user": "u1", "created": old, "heartbeat": old, "status": "working"})
    job = call(api, "GET", f"/api/jobs/{'f' * 32}")[1]
    assert job["status"] == "failed" and "try it again" in job["error"]
    assert aws.get_item(Key={"id": "f" * 32})["Item"]["status"] == "failed"


def test_a_song_whose_machine_spoke_up_just_now_is_not_marked_stopped(api, aws, monkeypatch):
    old = int(time.time()) - 3600
    aws.put_item(Item={"id": "e" * 32, "user": "u1", "created": old, "heartbeat": old, "status": "working"})
    real_get = api.TABLE.get_item

    def beat_after_reading(**kwargs):  # the worker's heartbeat lands between our read and our write
        item = real_get(**kwargs)
        aws.update_item(Key={"id": "e" * 32}, UpdateExpression="SET heartbeat = :now",
                        ExpressionAttributeValues={":now": int(time.time())})
        return item

    monkeypatch.setattr(api.TABLE, "get_item", beat_after_reading)
    assert call(api, "GET", f"/api/jobs/{'e' * 32}")[1]["status"] == "working"


def test_quick_failures_do_not_use_up_the_day_but_long_ones_do(api, aws):
    now = int(time.time())
    for n, (took, status) in enumerate([(30, "failed"), (30, "failed"), (30, "failed"), (900, "failed")]):
        job = "%032x" % (n + 1)
        aws.put_item(Item={"id": job, "user": "u1", "created": now - 1000, "finished": now - 1000 + took,
                           "status": status})
    # the person record is started from the songs of the last day
    assert call(api, "GET", "/api/me")[1]["left_today"] == 1  # only the 15-minute failure counts


def test_a_machine_that_is_still_starting_is_not_counted_as_stopped(api):
    now = time.time()
    assert api._being_made({"status": "waiting", "created": now - 10 * 60}, now)  # fetching its image
    assert not api._being_made({"status": "working", "created": now - 600, "heartbeat": now - 6 * 60}, now)
    assert api._being_made({"status": "working", "created": now - 3600, "heartbeat": now - 60}, now)


def test_uploads_must_be_the_persons_own_and_a_real_size(api):
    for key in ("uploads/u1/../u2/" + "a" * 32, "uploads/u1/" + "a" * 31, "uploads/u1/x/" + "a" * 32, "songs/u1/abc"):
        assert call(api, "POST", "/api/jobs", body={"upload_key": key})[0] == 400
    for size in (0, -5, "big"):
        assert call(api, "POST", "/api/uploads", body={"name": "a.mp3", "size": size})[0] == 400


def test_admins_see_which_emails_are_not_confirmed(api):
    claims = {"sub": "u9", "email": "new@example.com", "email_verified": "false"}
    assert api._user({"requestContext": {"authorizer": {"jwt": {"claims": claims}}}})["email"].endswith("(not confirmed)")
    claims["email_verified"] = "true"
    assert api._user({"requestContext": {"authorizer": {"jwt": {"claims": claims}}}})["email"] == "new@example.com"


def test_the_heartbeat_never_brings_back_a_stopped_song(aws, monkeypatch):
    from choir_solfa import cloud_worker

    aws.put_item(Item={"id": "d" * 32, "user": "u1", "status": "failed"})
    cloud_worker.update(aws, "d" * 32, while_active=True, heartbeat=1, progress=500)
    item = aws.get_item(Key={"id": "d" * 32})["Item"]
    assert item["status"] == "failed" and "heartbeat" not in item
    aws.put_item(Item={"id": "c" * 32, "user": "u1", "status": "working"})
    monkeypatch.setattr(cloud_worker, "HEARTBEAT_SECONDS", 0.01)
    with cloud_worker.Heartbeat(aws, "c" * 32):
        time.sleep(0.2)
    assert "heartbeat" in aws.get_item(Key={"id": "c" * 32})["Item"]


# ---------- the worker ----------

def test_worker_makes_the_song_and_cleans_up(aws, monkeypatch, tmp_path):
    from choir_solfa import cloud_worker, pipeline

    s3 = boto3.client("s3")
    s3.put_object(Bucket=BUCKET, Key="uploads/u1/x", Body=b"audio")
    job = {"id": "1" * 32, "user": "u1", "status": "waiting", "upload_key": "uploads/u1/x", "upload_name": "choir.mp3",
           "mode": "auto", "meter": "auto", "title_asked": ""}
    aws.put_item(Item=job)
    seen = {}

    class R:
        title = "My Choir"

        def to_dict(self):
            return {"title": self.title, "key": "Doh is C"}

    def fake_run(source, work, **kw):
        seen.update(source=Path(source).read_bytes(), **kw)
        work.mkdir(parents=True)
        for name in cloud_worker.FILES:
            (work / name).write_text(name)
        kw["progress"]("Separating the voices", 0.5)
        return R()

    monkeypatch.setattr(pipeline, "run", fake_run)
    cloud_worker.make_song(job, aws, s3, BUCKET, max_seconds=600)

    assert seen["source"] == b"audio" and seen["upload_name"] == "choir.mp3" and seen["max_seconds"] == 600
    item = aws.get_item(Key={"id": job["id"]})["Item"]
    assert item["status"] == "done" and item["title"] == "My Choir" and json.loads(item["result"])["key"] == "Doh is C"
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert keys == {f"songs/{job['id']}/{n}" for n in cloud_worker.FILES}  # the upload is gone
    head = s3.head_object(Bucket=BUCKET, Key=f"songs/{job['id']}/score.pdf")
    assert head["ContentType"] == "application/pdf"


def test_worker_reports_failures_in_plain_words(aws, monkeypatch):
    from choir_solfa import audio, cloud_worker, pipeline

    job = {"id": "2" * 32, "user": "u1", "status": "waiting", "url": "https://youtu.be/a"}
    aws.put_item(Item=job)

    def fake_run(source, work, **kw):
        raise audio.AudioError("This recording is 12 minutes long. Songs can be up to 10 minutes.")

    monkeypatch.setattr(pipeline, "run", fake_run)
    cloud_worker.make_song(job, aws, boto3.client("s3"), BUCKET, max_seconds=600)
    item = aws.get_item(Key={"id": job["id"]})["Item"]
    assert item["status"] == "failed" and "12 minutes" in item["error"]


def test_wireproxy_config_from_a_warp_profile():
    from choir_solfa.cloud_worker import wireproxy_config
    profile = ("[Interface]\nPrivateKey = abc\nAddress = 172.16.0.2/32\nMTU = 1280\n"
               "[Peer]\nPublicKey = xyz\nAllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = engage.cloudflareclient.com:2408\n")
    config = wireproxy_config(profile)
    assert "CheckAlive = 1.1.1.1" in config.split("[Peer]")[0]
    assert "Endpoint = 162.159.192.1:2408" in config and "engage" not in config
    assert config.rstrip().endswith("[http]\nBindAddress = 127.0.0.1:1080")


def test_link_songs_try_new_warp_addresses_until_youtube_lets_them_in(aws, monkeypatch):
    from choir_solfa import cloud_worker, fetch, pipeline

    job = {"id": "3" * 32, "user": "u1", "status": "waiting", "url": "https://youtu.be/a"}
    aws.put_item(Item=job)
    monkeypatch.setenv("USE_WARP", "1")
    tunnels = []

    class Tunnel:
        def terminate(self): self.closed = True
        def wait(self): pass

    def fake_warp(folder):
        tunnels.append(Tunnel())
        return tunnels[-1]

    def fake_run(source, work, **kw):
        if len(tunnels) < 3:
            raise fetch.FetchError(fetch.REFUSED)
        raise fetch.FetchError("That link is not a song.")

    monkeypatch.setattr(cloud_worker, "start_warp", fake_warp)
    monkeypatch.setattr(pipeline, "run", fake_run)
    cloud_worker.make_song(job, aws, boto3.client("s3"), BUCKET, max_seconds=600)
    item = aws.get_item(Key={"id": job["id"]})["Item"]
    assert len(tunnels) == 3 and all(t.closed for t in tunnels)
    assert item["status"] == "failed" and item["error"] == "That link is not a song."


# ---------- what Gitsentry raised ----------

def test_links_must_be_public_websites(api):
    ok = ["https://youtu.be/CZmC4qqL_Rg", "https://open.spotify.com/track/abc?si=1", "spotify:track:" + "a" * 22,
          "http://www.example.co.uk/song.mp3", "https://xn--bcher-kva.example/x"]
    bad = ["http://169.254.170.2/v2/credentials/x", "http://10.0.0.1/", "http://127.0.0.1:8080/", "http://[::1]/",
           "http://localhost/", "http://metadata.google.internal/", "http://2852039166/", "http://0xa9.0xfe.0xa9.0xfe/",
           "http://intranet/", "http://printer.local/", "ftp://example.com/a", "https://example.com:99999/", "spotify:track:x"]
    assert [u for u in ok if not api.public_link(u)] == []
    assert [u for u in bad if api.public_link(u)] == []
    assert call(api, "POST", "/api/jobs", body={"url": "http://169.254.169.254/latest/meta-data/"})[0] == 400


def test_names_that_lead_inside_are_refused_before_a_machine_starts(api):
    answers = {"inside.example.com": "169.254.169.254", "vpc.example.com": "10.0.4.2", "loop.example.com": "127.0.0.1",
               "v6.example.com": "fd00::1", "fine.example.com": "93.184.215.14"}

    def lookup(host):
        if host not in answers:
            raise socket.gaierror("no such name")
        return [(2, 1, 6, "", (answers[host], 0))]

    api._lookup = lookup
    for name in ("inside", "vpc", "loop", "v6"):
        status, body = call(api, "POST", "/api/jobs", body={"url": f"https://{name}.example.com/song"})
        assert status == 400 and "public website" in body["detail"], name
    assert api.ECS.started == []
    assert not api.points_inside("https://fine.example.com/song")
    assert not api.points_inside("https://unknown.example.com/song")  # left to the download to report
    assert not api.points_inside("spotify:track:" + "a" * 22)


def test_a_slow_lookup_does_not_hold_up_the_request(api):
    api._lookup = lambda host: time.sleep(2) or []
    started = time.time()
    assert not api.points_inside("https://slow.example.com/song", wait=0.2)
    assert time.time() - started < 1


def test_one_person_can_only_have_two_songs_being_made(api, monkeypatch):
    monkeypatch.setattr(api, "DAILY_LIMIT", 10)
    for _ in range(2):
        assert call(api, "POST", "/api/jobs", body={"url": "https://youtu.be/a"})[0] == 200
    status, err = call(api, "POST", "/api/jobs", body={"url": "https://youtu.be/a"})
    assert status == 429 and "wait for one to finish" in err["detail"]
    assert call(api, "POST", "/api/jobs", user="boss", admin=True, body={"url": "https://youtu.be/a"})[0] == 200


def test_songs_started_at_the_same_moment_cannot_pass_the_limit_together(api, aws, monkeypatch):
    """Another request starts a song between our count and our write: ours must count again."""
    real_usage = api._usage
    sneaked = []

    def counts_then_race(user):
        result = real_usage(user)
        if not sneaked:  # the other request saves its song and moves the gate on
            sneaked.append(1)
            call(api, "POST", "/api/jobs", body={"url": "https://youtu.be/b"})
        return result

    monkeypatch.setattr(api, "_usage", counts_then_race)
    monkeypatch.setattr(api, "MAX_RUNNING_EACH", 10)
    assert call(api, "POST", "/api/jobs", body={"url": "https://youtu.be/a"})[0] == 200  # 2 of 2: allowed after recounting
    monkeypatch.setattr(api, "_usage", real_usage)
    assert call(api, "POST", "/api/jobs", body={"url": "https://youtu.be/c"})[0] == 429
    songs = [i for i in aws.scan()["Items"] if not i["id"].startswith("_")]
    assert len(songs) == 2 and int(aws.get_item(Key={"id": api.GATE})["Item"]["v"]) == 2
    assert int(aws.get_item(Key={"id": "_person#u1"})["Item"]["v"]) == 2
    rows = call(api, "GET", "/api/admin/songs", user="boss", admin=True)[1]
    assert len(rows) == 2  # the gate record isn't listed as a song


def test_links_are_never_fetched_without_the_tunnel(aws, monkeypatch):
    from choir_solfa import cloud_worker, pipeline

    job = {"id": "4" * 32, "user": "u1", "status": "waiting", "url": "https://youtu.be/a"}
    aws.put_item(Item=job)
    monkeypatch.setenv("USE_WARP", "1")
    fetched = []
    monkeypatch.setattr(cloud_worker, "start_warp", lambda folder: None)
    monkeypatch.setattr(pipeline, "run", lambda *a, **kw: fetched.append(1))
    cloud_worker.make_song(job, aws, boto3.client("s3"), BUCKET, max_seconds=600)
    item = aws.get_item(Key={"id": job["id"]})["Item"]
    assert fetched == [] and item["status"] == "failed" and item["error"] == cloud_worker.NO_TUNNEL


def test_unexpected_errors_do_not_show_internal_details(aws, monkeypatch):
    from choir_solfa import cloud_worker, pipeline

    job = {"id": "5" * 32, "user": "u1", "status": "waiting", "url": "https://youtu.be/a"}
    aws.put_item(Item=job)

    def fake_run(source, work, **kw):
        raise OSError("[Errno 2] No such file: /tmp/tmpab12/job/vocals.wav in bucket choir-solfa-data")

    monkeypatch.setattr(pipeline, "run", fake_run)
    cloud_worker.make_song(job, aws, boto3.client("s3"), BUCKET, max_seconds=600)
    item = aws.get_item(Key={"id": job["id"]})["Item"]
    assert item["error"] == cloud_worker.SOMETHING_WRONG


# ---------- signing in through /api/auth ----------

class FakeCognito:
    """Stands in for Cognito's token endpoint: knows one code and one renewal token."""
    def __init__(self):
        self.calls = []
        self.dark = []

    def __call__(self, page, fields, dark=False):
        import io
        import urllib.error
        self.calls.append((page, fields))
        self.dark.append(dark)
        if page == "oauth2/revoke":
            return b""
        if fields.get("code") == "good-code" or fields.get("refresh_token") == "renew-1":
            tokens = {"id_token": "id-1"}
            if fields.get("code"):
                tokens["refresh_token"] = "renew-1"
            return json.dumps(tokens).encode()
        raise urllib.error.HTTPError("https://auth", 400, "invalid_grant", {}, io.BytesIO(b"{}"))


def sign_in_call(api, action, body=None, cookies=None, content_type="application/json"):
    event = {"rawPath": f"/api/auth/{action}", "requestContext": {"http": {"method": "POST"}},
             "headers": {"content-type": content_type}, "cookies": cookies or [],
             "body": json.dumps(body or {})}
    res = api.handler(event, None)  # no authorizer claims: these routes are open
    return res["statusCode"], json.loads(res["body"]), res.get("cookies", [])


@pytest.fixture
def cognito(api, monkeypatch):
    fake = FakeCognito()
    monkeypatch.setattr(api, "_cognito", fake)
    return fake


def test_signing_in_keeps_the_renewal_token_in_a_cookie_the_page_cannot_read(api, cognito):
    code = {"code": "good-code", "code_verifier": "v" * 43, "redirect_uri": "https://solfa.example/"}
    status, data, cookies = sign_in_call(api, "token", code)
    assert status == 200 and data == {"id": "id-1"}  # the renewal token isn't in the reply
    (cookie,) = cookies
    assert cookie.startswith("__Secure-solfa-renew=renew-1;")
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/api/auth", f"Max-Age={30 * 86400}"):
        assert flag in cookie
    assert cognito.calls[0][1]["grant_type"] == "authorization_code"

    status, _, _ = sign_in_call(api, "token", {**code, "code": "used-code"})
    assert status == 400
    status, _, _ = sign_in_call(api, "token", {"code": "good-code"})
    assert status == 400


def test_renewing_uses_the_cookie_and_a_bad_one_is_cleared(api, cognito):
    status, data, cookies = sign_in_call(api, "renew", cookies=["__Secure-solfa-renew=renew-1"])
    assert status == 200 and data == {"id": "id-1"} and cookies == []

    status, data, cookies = sign_in_call(api, "renew", cookies=["other=1", "__Secure-solfa-renew=expired"])
    assert status == 401 and data["detail"] == api.SIGN_IN_AGAIN
    assert cookies and "Max-Age=0" in cookies[0]

    status, _, cookies = sign_in_call(api, "renew")  # never signed in
    assert status == 401 and cookies == []


def test_signing_out_revokes_and_clears_the_cookie(api, cognito):
    status, _, cookies = sign_in_call(api, "signout", cookies=["__Secure-solfa-renew=renew-1"])
    assert status == 200 and "Max-Age=0" in cookies[0]
    assert cognito.calls == [("oauth2/revoke", {"token": "renew-1"})]


def test_signing_in_on_the_dark_page_keeps_using_the_dark_client(api, cognito, monkeypatch):
    # The sign-in page matches the site's light / dark switch through a second client; the
    # renewal token must go back to the client that gave it.
    monkeypatch.setattr(api, "CLIENT_ID_DARK", "dark-client")
    code = {"code": "good-code", "code_verifier": "v" * 43, "redirect_uri": "https://solfa.example/", "look": "dark"}
    status, _, (cookie,) = sign_in_call(api, "token", code)
    assert status == 200 and cookie.startswith("__Secure-solfa-renew=dark:renew-1;")
    status, data, _ = sign_in_call(api, "renew", cookies=["__Secure-solfa-renew=dark:renew-1"])
    assert status == 200 and data == {"id": "id-1"}
    sign_in_call(api, "signout", cookies=["__Secure-solfa-renew=dark:renew-1"])
    assert cognito.dark == [True, True, True]
    assert cognito.calls[-1] == ("oauth2/revoke", {"token": "renew-1"})

    # Light, or a cookie from before there were two clients: the first client.
    sign_in_call(api, "renew", cookies=["__Secure-solfa-renew=renew-1"])
    sign_in_call(api, "token", {**code, "look": "light"})
    assert cognito.dark[-2:] == [False, False]


def test_other_websites_cannot_use_the_sign_in_routes(api, cognito):
    # A form on another site can only send text/plain or form data, not JSON.
    status, _, cookies = sign_in_call(api, "signout", cookies=["__Secure-solfa-renew=renew-1"],
                                      content_type="text/plain")
    assert status == 415 and cookies == [] and cognito.calls == []
    status, _, _ = sign_in_call(api, "whoami")
    assert status == 404


# ---------- links that lead inside ----------

@pytest.mark.parametrize("address, public", [
    ("142.250.80.46", True), ("2607:f8b0:4004:c07::64", True),
    ("127.0.0.1", False), ("10.0.3.7", False), ("169.254.169.254", False), ("192.168.1.1", False),
    ("100.64.0.1", False), ("::1", False), ("fd00::1", False), ("0.0.0.0", False),
])
def test_links_whose_name_leads_to_a_private_address_are_refused(monkeypatch, address, public):
    import socket
    from choir_solfa import cloud_worker

    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **kw: [(family, 1, 6, "", (address, 0))])
    assert cloud_worker.public_address("https://music.example.com/song") is public
    assert cloud_worker.public_address("spotify:track:4uLU6hMCjMI75M1A2tKUQC") is True


def test_a_private_link_is_never_fetched(aws, monkeypatch):
    from choir_solfa import cloud_worker, pipeline

    job = {"id": "6" * 32, "user": "u1", "status": "waiting", "url": "https://inside.example.com/a"}
    aws.put_item(Item=job)
    fetched = []
    monkeypatch.setattr(cloud_worker, "public_address", lambda url: False)
    monkeypatch.setattr(cloud_worker, "start_warp", lambda folder: fetched.append("warp"))
    monkeypatch.setattr(pipeline, "run", lambda *a, **kw: fetched.append("run"))
    cloud_worker.make_song(job, aws, boto3.client("s3"), BUCKET, max_seconds=600)
    item = aws.get_item(Key={"id": job["id"]})["Item"]
    assert fetched == [] and item["status"] == "failed" and item["error"] == cloud_worker.NOT_PUBLIC
