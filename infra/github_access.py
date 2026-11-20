"""Lets GitHub Actions deploy the site, with no AWS keys stored anywhere.

GitHub signs each workflow run with a short-lived token (OpenID Connect). This role accepts only
tokens for this repository's "production" environment, which only the main branch may use. All it
may do is hand the deploy over to CDK's own bootstrap roles and read this deployment's settings.

Deploy once by hand (cd infra && npx aws-cdk@2 deploy ChoirSolfaGitHub); after that, merging to
main deploys ChoirSolfa (.github/workflows/deploy.yml).
"""
from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

SETTINGS_PARAMETER = "/choir-solfa/deploy-context"  # infra/cdk.context.json, kept in SSM as a SecureString
ISSUER = "token.actions.githubusercontent.com"


class GitHubDeployStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, repo: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)
        provider = iam.OidcProviderNative(self, "GitHub", url=f"https://{ISSUER}", client_ids=["sts.amazonaws.com"])
        role = iam.Role(
            self, "Deploy", role_name="choir-solfa-github-deploy", max_session_duration=Duration.hours(2),
            description=f"GitHub Actions in {repo} (production environment) deploying Choir Solfa",
            assumed_by=iam.WebIdentityPrincipal(provider.oidc_provider_arn, conditions={
                "StringEquals": {f"{ISSUER}:aud": "sts.amazonaws.com",
                                 f"{ISSUER}:sub": f"repo:{repo}:environment:production"}}))
        # CDK's bootstrap roles (deploy, publish files and images, look up) do the actual work.
        role.add_to_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole", "sts:TagSession"],
            resources=[f"arn:aws:iam::{self.account}:role/cdk-hnb659fds-*-{self.account}-{self.region}"]))
        role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter{SETTINGS_PARAMETER}"]))
        CfnOutput(self, "DeployRoleArn", value=role.role_arn,
                  description="Store as the AWS_DEPLOY_ROLE secret of the GitHub environment 'production'")
