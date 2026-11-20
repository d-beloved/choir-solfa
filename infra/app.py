#!/usr/bin/env python3
"""Choir Solfa on AWS. Merging to main deploys it (.github/workflows/deploy.yml); by hand:
    cd infra && npx aws-cdk@2 deploy ChoirSolfa

Settings can be changed in cdk.context.json (gitignored, which holds this deployment's
addresses and alert email) or on the command line, e.g.
    npx aws-cdk@2 deploy -c daily_limit=10 -c alert_email=you@example.com
"""
import os

import aws_cdk as cdk

from github_access import GitHubDeployStack
from stack import ChoirSolfaStack

DEFAULTS = {
    "keep_days": 15,          # scores are deleted this many days after they're made (the records stay)
    "daily_limit": 5,         # songs per person per 24 hours (admins have no limit)
    "max_running": 5,         # songs being made at the same time, across everyone
    "max_running_each": 2,    # songs one person can have being made at the same time (admins have no limit)
    "api_rate": 20,           # API requests per second across everyone (bursts up to twice that)
    "max_song_minutes": 10,
    "max_upload_mb": 100,
    "worker_cpu": 4096,       # 4 vCPU
    "worker_memory": 12288,   # 12 GB
    "alert_usd": 10,
    "alert_email": "",        # where to email when a month's spending is forecast to pass, or passes, alert_usd.
                              # Kept out of the repository: put it in cdk.context.json (gitignored) or pass
                              # -c alert_email=you@example.com. "none" deploys without the alert.
    # This deployment's own names live in cdk.context.json (gitignored), not here:
    "domain": "",                 # the site's address, e.g. solfa.example.com; needs certificate_arn
    "certificate_arn": "",        # an ACM certificate for domain, in us-east-1
    "login_domain": "",           # the sign-in page's address, e.g. auth.solfa.example.com; needs login_certificate_arn
    "login_certificate_arn": "",  # an ACM certificate for login_domain, in us-east-1
    "login_prefix": "",           # Cognito's own address for the sign-in page: https://<prefix>.auth.<region>.amazoncognito.com
    "google_client_id": "",       # turns on "Continue with Google"
    "google_stored_as": "choir-solfa/google-client-secret",  # the name its client secret is stored under in Secrets Manager (not the secret itself: see README)
    "local_page": False,
    "github_repo": "d-beloved/choir-solfa",  # whose main branch deploys the site (github_access.py)
}

app = cdk.App()
settings = {k: app.node.try_get_context(k) if app.node.try_get_context(k) is not None else v for k, v in DEFAULTS.items()}
if not settings["login_prefix"]:
    raise SystemExit("Set login_prefix (in infra/cdk.context.json or with -c login_prefix=...): the start of "
                     "Cognito's own sign-in address. Changing it on a live site replaces that address.")
for name, cert in (("domain", "certificate_arn"), ("login_domain", "login_certificate_arn")):
    if settings[name] and not settings[cert]:
        raise SystemExit(f"{name} is set but {cert} isn't. Add it to infra/cdk.context.json.")
if not settings["alert_email"]:
    raise SystemExit("Set alert_email (in infra/cdk.context.json or with -c alert_email=...) so a spending alert is "
                     "kept, or -c alert_email=none to deploy without one.")
env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=os.environ.get("CDK_DEFAULT_REGION"))
ChoirSolfaStack(app, "ChoirSolfa", settings=settings, env=env)
GitHubDeployStack(app, "ChoirSolfaGitHub", repo=settings["github_repo"], env=env)
app.synth()
