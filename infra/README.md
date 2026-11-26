# Choir Solfa on AWS

The website version: people sign in, make songs from a link or an upload, and come back to them
from any phone or computer. Nothing runs while nobody is making a song.

```
browser ──► CloudFront ──► S3 (the page)
                └─ /api/* ──► API Gateway (checks the sign-in) ──► Lambda (cloud/api/handler.py)
                                                                     ├─ DynamoDB: song records
                                                                     ├─ S3: uploads and scores
                                                                     └─ starts one Fargate task per song
Fargate task (cloud/worker.Dockerfile, python -m choir_solfa.cloud_worker): makes the song,
writes its progress and the PDF, text and practice tracks, then stops.
```

Everything is defined in `stack.py`, with the settings in `app.py`, and created by
`npx aws-cdk@2 deploy`.

- **The page** is `src/choir_solfa/web/index.html`, the same page the local app serves. The deploy
  fills in its sign-in and API settings and copies it to S3 with the privacy policy and logo files.
- **Sign-in** is Amazon Cognito: email and password, and optionally Google. Each person sees only
  their own songs. People in the Cognito group `admins` have no daily limit and can see all songs.
- **Making a song** runs the same pipeline as the local app, in its own Fargate task.
- **Score files** are deleted after `keep_days` days. Uploads are deleted once the song is made.
  The song list stays.

## Settings

Limits and sizes are in `app.py` and can be changed with `-c name=value` on the command line.

This deployment's own names and addresses go in `infra/cdk.context.json`, which is kept out of git:

```json
{
  "alert_email": "you@example.com",
  "login_prefix": "choirsolfa-xxxx",
  "domain": "solfa.example.com",
  "certificate_arn": "arn:aws:acm:us-east-1:…",
  "login_domain": "auth.solfa.example.com",
  "login_certificate_arn": "arn:aws:acm:us-east-1:…",
  "google_client_id": "….apps.googleusercontent.com"
}
```

- `alert_email` gets an email when the month's AWS spending is forecast to pass `alert_usd`.
  Use `none` to go without.
- `login_prefix` names Cognito's own sign-in address. Changing it on a live site replaces that
  address.
- `domain` and `login_domain` are optional, and each needs its certificate. `google_client_id` is
  optional.

## Deploy

**Merging to main deploys the site.** `.github/workflows/deploy.yml` runs the tests, then
`cdk deploy ChoirSolfa` on an ARM runner. GitHub signs in to AWS with a short-lived token. No AWS
keys are stored, and the role it gets (`github_access.py`) trusts only this repository's
`production` environment, which only main may use. The settings above are kept in AWS Systems
Manager, not in the repository.

To set this up, run the script once. Run it again whenever you change `infra/cdk.context.json`.
You need Node 22+, the AWS CLI signed in to the account, and the GitHub CLI.

```bash
AWS_PROFILE=<profile> infra/setup-github-deploy.sh
```

For a brand-new account, bootstrap CDK first. You need Docker to deploy by hand:

```bash
cd infra
uv venv .venv && uv pip install --python .venv -r requirements.txt
npx aws-cdk@2 bootstrap        # once per account and region
npx aws-cdk@2 deploy ChoirSolfa
```

The worker image is built for ARM. It installs exact package versions, checked against their
hashes, from `cloud/worker-requirements.txt` and `cloud/worker-torch.txt`. After changing
dependencies, run `cloud/lock-worker.sh` to remake them, then build the image and make a song
with it.

The outputs show the site address (`SiteUrl`), the user pool and the sign-in page's DNS target.

## Admins

After someone signs up, find their user name (an id, not the email) and add it to the group:

```bash
aws cognito-idp list-users --user-pool-id <UserPoolId> --filter 'email = "them@example.com"' --query 'Users[].Username'
aws cognito-idp admin-add-user-to-group --user-pool-id <UserPoolId> --username <that id> --group-name admins
```

They need to sign out and in again. `admin-remove-user-from-group` takes it away.

## Google sign-in

1. In the Google Cloud console, open **Google Auth Platform** and set up the branding with your
   home page, `/privacy.html` as the privacy policy, and your domain. Publish the app (the basic
   email and profile scopes need no review).
2. Create a **Web application** client. Its authorized JavaScript origin is your sign-in address,
   for example `https://auth.solfa.example.com`. Its redirect URI is that address plus
   `/oauth2/idpresponse`.
3. Store the client secret in AWS Secrets Manager under the name in `google_stored_as` (`app.py`).
   One way is the console: Secrets Manager → Store a new secret → Other type of secret →
   Plaintext. Another is a terminal, pasting the secret at a hidden prompt (zsh) so it stays out
   of the shell history:

   ```bash
   read -rs "?Paste the Google client secret: " && print -rn -- "$REPLY" | aws secretsmanager create-secret --name choir-solfa/google-client-secret --secret-string file:///dev/stdin; unset REPLY
   ```

4. Put the client ID in `google_client_id` and deploy.

## Custom domain

CloudFront and Cognito only use certificates from us-east-1, so request both certificates in
us-east-1 and validate them with DNS. Point `domain` at the CloudFront distribution. Point
`login_domain` at the `LoginDnsTarget` output with a CNAME. If your DNS is on Cloudflare, use
DNS only, not proxied.

## Notes

- Cognito's built-in email sends about 50 emails a day. If more people sign up than that,
  connect Amazon SES to the user pool.
- The table, the data bucket and the user pool are kept if the stack is deleted.
- Only the three newest worker images are kept in CDK's image store; older ones are deleted.
