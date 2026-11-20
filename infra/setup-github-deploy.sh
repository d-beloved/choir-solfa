#!/usr/bin/env bash
# Let GitHub Actions deploy the site whenever main changes (.github/workflows/deploy.yml).
# Run once, and again whenever you change infra/cdk.context.json:
#     AWS_PROFILE=<your profile> infra/setup-github-deploy.sh
# Needs: Node 22+ (npx), the AWS CLI signed in to the account, and the GitHub CLI signed in.
set -euo pipefail
cd "$(dirname "$0")"

repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)
[ -f cdk.context.json ] || { echo "infra/cdk.context.json is missing (see infra/README.md)."; exit 1; }
[ -x .venv/bin/python ] || { uv venv --python 3.11 .venv && uv pip install --python .venv -r requirements.txt; }

echo "1/4 The role GitHub signs in to AWS with (no keys)"
# How GitHub names this repository in its tokens: repo:OWNER/NAME, or with ids for newer repositories.
subject=$(gh api "repos/$repo/actions/oidc/customization/sub" -q '.sub_claim_prefix // empty' || true)
[ -n "$subject" ] || echo "    GitHub didn't say; using repo:$repo"
npx --yes aws-cdk@2.1149.0 deploy ChoirSolfaGitHub --require-approval never \
  -c github_subject="${subject:-repo:$repo}" >/dev/null
role=$(aws cloudformation describe-stacks --stack-name ChoirSolfaGitHub \
  --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue" --output text)

echo "2/4 This deployment's settings, kept in AWS rather than in the public repository"
aws ssm put-parameter --name /choir-solfa/deploy-context --type SecureString --overwrite \
  --value "file://cdk.context.json" >/dev/null

echo "3/4 The GitHub environment 'production', usable from main only"
gh api -X PUT "repos/$repo/environments/production" --silent --input - <<'JSON'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
if ! gh api "repos/$repo/environments/production/deployment-branch-policies" -q '.branch_policies[].name' | grep -qx main; then
  gh api -X POST "repos/$repo/environments/production/deployment-branch-policies" -f name=main -f type=branch --silent
fi

echo "4/4 The role's name, as the environment's secret"
printf '%s' "$role" | gh secret set AWS_DEPLOY_ROLE --env production --repo "$repo"

echo "Done. Merging to main now tests and deploys the site."
