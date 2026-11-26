"""Everything Choir Solfa needs on AWS, made with one `cdk deploy`.

    phone or computer ──> CloudFront ──> S3 (the page)
                                └─ /api/* ─> API Gateway (checks the Cognito sign-in) ─> Lambda (cloud/api)
                                                                                          │
              DynamoDB (song records, kept) <── Fargate task, one per song <── run_task ──┘
              S3 (uploads and scores, deleted after KEEP_DAYS)

Nothing runs while nobody is using it: Fargate tasks stop when their song is done, and
Lambda, API Gateway, DynamoDB and S3 charge per request or per GB stored.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from aws_cdk import CfnOutput, Duration, Fn, RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import custom_resources as cr
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "src" / "choir_solfa" / "web" / "index.html"
PRIVACY = ROOT / "src" / "choir_solfa" / "web" / "privacy.html"
ICON = ROOT / "src" / "choir_solfa" / "assets" / "icon.png"


def script_hashes(html: str) -> list[str]:
    """CSP hashes of the page's inline scripts (plain <script> tags; the JSON settings block isn't run)."""
    return [f"'sha256-{base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()}'"
            for code in re.findall(r"<script>(.*?)</script>", html, flags=re.S)]


class ChoirSolfaStack(Stack):
    def __init__(self, scope: Construct, id: str, *, settings: dict, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)
        keep_days = int(settings["keep_days"])
        domain = settings.get("domain") or None          # e.g. solfa.example.com, once its certificate exists
        certificate_arn = settings.get("certificate_arn") or None

        # ---------- storage ----------
        table = dynamodb.Table(
            self, "Songs", partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST, removal_policy=RemovalPolicy.RETAIN)
        table.add_global_secondary_index(
            index_name="by-user", partition_key=dynamodb.Attribute(name="user", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created", type=dynamodb.AttributeType.NUMBER))

        site_bucket = s3.Bucket(self, "Site", block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                                enforce_ssl=True, removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)

        # ---------- the website address ----------
        distribution = cloudfront.Distribution(
            self, "Web",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED, compress=True),
            default_root_object="index.html",
            domain_names=[domain] if domain and certificate_arn else None,
            certificate=acm.Certificate.from_certificate_arn(self, "Cert", certificate_arn) if domain and certificate_arn else None,
            price_class=cloudfront.PriceClass.PRICE_CLASS_ALL,
            comment="Choir Solfa")
        origins_allowed = [f"https://{distribution.distribution_domain_name}"] + ([f"https://{domain}"] if domain else [])
        site_urls = [o + "/" for o in origins_allowed] + (["http://localhost:4795/"] if settings.get("local_page") else [])

        data_bucket = s3.Bucket(
            self, "Data", block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED, removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(id="scores", prefix="songs/", expiration=Duration.days(keep_days + 1)),
                s3.LifecycleRule(id="uploads", prefix="uploads/", expiration=Duration.days(2)),
                s3.LifecycleRule(id="unfinished", abort_incomplete_multipart_upload_after=Duration.days(1)),
            ],
            cors=[s3.CorsRule(allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.POST],
                              allowed_origins=origins_allowed + (["http://localhost:4795"] if settings.get("local_page") else []),
                              allowed_headers=["*"], max_age=3600)])

        # ---------- sign-in ----------
        pool = cognito.UserPool(
            self, "Users", user_pool_name="choir-solfa", self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True), auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(email=cognito.StandardAttribute(required=True, mutable=True)),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY, feature_plan=cognito.FeaturePlan.ESSENTIALS,
            # A changed email only takes effect once its code is entered, so nobody can show
            # (to admins) an address they don't own.
            keep_original=cognito.KeepOriginalAttrs(email=True),
            # Email and password. (With email codes allowed too, Cognito's page always sends a code first.)
            sign_in_policy=cognito.SignInPolicy(allowed_first_auth_factors=cognito.AllowedFirstAuthFactors(password=True)),
            password_policy=cognito.PasswordPolicy(min_length=8, require_lowercase=False, require_uppercase=False,
                                                   require_digits=False, require_symbols=False),
            user_verification=cognito.UserVerificationConfig(
                email_subject="Your Choir Solfa code", email_body="Your Choir Solfa code is {####}",
                email_style=cognito.VerificationEmailStyle.CODE),
            removal_policy=RemovalPolicy.RETAIN)
        # "Continue with Google" when google_client_id is set; its secret is in Secrets Manager (see README).
        providers = [cognito.UserPoolClientIdentityProvider.COGNITO]
        google = None
        if settings.get("google_client_id"):
            google = cognito.UserPoolIdentityProviderGoogle(
                self, "Google", user_pool=pool, client_id=settings["google_client_id"],
                # Only the secret's name in Secrets Manager is here; its value is stored there (see README).
                client_secret_value=SecretValue.secrets_manager(settings["google_stored_as"]),
                scopes=["openid", "email", "profile"],
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.GOOGLE_EMAIL, fullname=cognito.ProviderAttribute.GOOGLE_NAME,
                    custom={"email_verified": cognito.ProviderAttribute.other("email_verified")}))
            providers.append(cognito.UserPoolClientIdentityProvider.GOOGLE)
        # Two clients that differ only in how the sign-in page looks: the page sends people to the
        # one that matches its light / dark switch, since Cognito's page can't be told which to show.
        def web_client(name: str) -> cognito.UserPoolClient:
            return pool.add_client(
                name, generate_secret=False, auth_flows=cognito.AuthFlow(user_srp=True),
                o_auth=cognito.OAuthSettings(
                    flows=cognito.OAuthFlows(authorization_code_grant=True),
                    scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                    callback_urls=site_urls, logout_urls=site_urls),
                supported_identity_providers=providers,
                id_token_validity=Duration.hours(1), access_token_validity=Duration.hours(1),
                refresh_token_validity=Duration.days(30), prevent_user_existence_errors=True)
        client, dark_client = web_client("Web"), web_client("WebDark")
        login = pool.add_domain(
            "Login", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=settings["login_prefix"]),
            managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN)
        # The sign-in page's own address (e.g. auth.solfa.example.com), so Google and the browser
        # show that rather than amazoncognito.com. Its certificate must be in us-east-1.
        auth_domain = None
        if settings.get("login_domain") and settings.get("login_certificate_arn"):
            auth_domain = pool.add_domain(
                "LoginAddress", managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
                custom_domain=cognito.CustomDomainOptions(
                    domain_name=settings["login_domain"],
                    certificate=acm.Certificate.from_certificate_arn(self, "LoginCert", settings["login_certificate_arn"])))
            CfnOutput(self, "LoginDnsTarget", value=auth_domain.cloud_front_endpoint,
                      description=f"CNAME {settings['login_domain']} to this (DNS only)")
        # One account per email: a Google sign-in joins the password account with the same email.
        linker = lambda_.Function(
            self, "LinkAccounts", runtime=lambda_.Runtime.PYTHON_3_12, architecture=lambda_.Architecture.ARM_64,
            handler="link.handler", code=lambda_.Code.from_asset(str(ROOT / "cloud" / "signin")),
            timeout=Duration.seconds(10),
            log_group=logs.LogGroup(self, "LinkLogs", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY))
        # Any pool in this account: naming this pool here would make the pool and its trigger depend on each other.
        linker.add_to_role_policy(iam.PolicyStatement(
            actions=["cognito-idp:ListUsers", "cognito-idp:AdminLinkProviderForUser"],
            resources=[f"arn:aws:cognito-idp:{self.region}:{self.account}:userpool/*"]))
        pool.add_trigger(cognito.UserPoolOperation.PRE_SIGN_UP, linker)
        # The sign-in page in the site's colours, with its icon (login_look.json: Cognito's settings
        # format), light for one client and dark for the other.
        logo = base64.b64encode((ROOT / "src" / "choir_solfa" / "assets" / "icon.png").read_bytes()).decode()
        for name, web, mode in (("LoginLook", client, "LIGHT"), ("LoginLookDark", dark_client, "DARK")):
            look = json.loads((Path(__file__).parent / "login_look.json").read_text())
            look["categories"]["global"]["colorSchemeMode"] = mode
            branding = cognito.CfnManagedLoginBranding(
                self, name, user_pool_id=pool.user_pool_id, client_id=web.user_pool_client_id, settings=look,
                assets=[cognito.CfnManagedLoginBranding.AssetTypeProperty(
                    category="FORM_LOGO", color_mode=mode, extension="PNG", bytes=logo)])
            branding.node.add_dependency(login)
            if auth_domain:
                branding.node.add_dependency(auth_domain)
            if google:
                web.node.add_dependency(google)
        cognito.CfnUserPoolGroup(self, "Admins", user_pool_id=pool.user_pool_id, group_name="admins",
                                 description="Can see every song on the admin list")

        # ---------- the song maker: one Fargate task per song ----------
        vpc = ec2.Vpc(self, "Net", max_azs=2, nat_gateways=0,  # public subnets only: no NAT gateway to pay for
                      subnet_configuration=[ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)])
        worker_sg = ec2.SecurityGroup(self, "WorkerNet", vpc=vpc, allow_all_outbound=True,
                                      description="Song tasks: outgoing only (downloads, AWS)")
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)
        task = ecs.FargateTaskDefinition(
            self, "Worker", cpu=int(settings["worker_cpu"]), memory_limit_mib=int(settings["worker_memory"]),
            ephemeral_storage_gib=30,
            runtime_platform=ecs.RuntimePlatform(cpu_architecture=ecs.CpuArchitecture.ARM64,
                                                 operating_system_family=ecs.OperatingSystemFamily.LINUX))
        image = ecr_assets.DockerImageAsset(self, "WorkerImage", directory=str(ROOT), file="cloud/worker.Dockerfile",
                                            platform=ecr_assets.Platform.LINUX_ARM64)
        # Each change to the worker pushes a new image of about 1 GB, and CDK's image store keeps
        # them all. Keep the newest few: the worker in use is always among them, and the rest
        # cover a deploy that is rolled back. Naming the newest image in the rule sets it again
        # with every new image, in case `cdk bootstrap` puts back its own rule.
        cr.AwsCustomResource(
            self, "KeepRecentWorkerImages",
            on_update=cr.AwsSdkCall(
                service="ECR", action="putLifecyclePolicy",
                parameters={"repositoryName": image.repository.repository_name,
                            "lifecyclePolicyText": json.dumps({"rules": [{
                                "rulePriority": 1, "description": f"Keep the 3 newest images (newest: {image.image_tag})",
                                "selection": {"tagStatus": "any", "countType": "imageCountMoreThan",
                                              "countNumber": 3},
                                "action": {"type": "expire"}}]})},
                physical_resource_id=cr.PhysicalResourceId.of("keep-recent-worker-images")),
            policy=cr.AwsCustomResourcePolicy.from_statements([iam.PolicyStatement(
                actions=["ecr:PutLifecyclePolicy"], resources=[image.repository.repository_arn])]),
            install_latest_aws_sdk=False)
        task.add_container(
            "worker", image=ecs.ContainerImage.from_docker_image_asset(image),
            logging=ecs.LogDriver.aws_logs(stream_prefix="song", log_retention=logs.RetentionDays.ONE_MONTH),
            environment={"TABLE": table.table_name, "BUCKET": data_bucket.bucket_name,
                         "MAX_SONG_MINUTES": str(settings["max_song_minutes"]), "MAX_TASK_MINUTES": "60",
                         "USE_WARP": "1"})
        # Only what a song needs: its own record (read it, update it) and the files (read and
        # delete the upload, write the score). It can't list, scan or delete records.
        task.task_role.add_to_principal_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"], resources=[table.table_arn]))
        task.task_role.add_to_principal_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:DeleteObject"], resources=[data_bucket.arn_for_objects("uploads/*")]))
        task.task_role.add_to_principal_policy(iam.PolicyStatement(
            actions=["s3:PutObject"], resources=[data_bucket.arn_for_objects("songs/*")]))

        # ---------- the API ----------
        api_fn = lambda_.Function(
            self, "Api", runtime=lambda_.Runtime.PYTHON_3_12, architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler", code=lambda_.Code.from_asset(str(ROOT / "cloud" / "api")),
            timeout=Duration.seconds(20), memory_size=256,
            log_group=logs.LogGroup(self, "ApiLogs", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY),
            environment={
                "TABLE": table.table_name, "BUCKET": data_bucket.bucket_name, "KEEP_DAYS": str(keep_days),
                "DAILY_LIMIT": str(settings["daily_limit"]), "MAX_RUNNING": str(settings["max_running"]),
                "MAX_RUNNING_EACH": str(settings["max_running_each"]),
                "MAX_UPLOAD_MB": str(settings["max_upload_mb"]), "MAX_SONG_MINUTES": str(settings["max_song_minutes"]),
                "CLUSTER": cluster.cluster_arn, "TASK_DEFINITION": task.task_definition_arn, "CONTAINER": "worker",
                "SUBNETS": ",".join(s.subnet_id for s in vpc.public_subnets), "SECURITY_GROUP": worker_sg.security_group_id,
                # For /api/auth, which swaps sign-in codes for tokens and keeps the renewal one in a cookie.
                "AUTH_DOMAIN": (auth_domain or login).base_url(), "CLIENT_ID": client.user_pool_client_id,
                "CLIENT_ID_DARK": dark_client.user_pool_client_id,
            })
        table.grant_read_write_data(api_fn)
        data_bucket.grant_read_write(api_fn)
        data_bucket.grant_delete(api_fn)
        task.grant_run(api_fn)

        http_api = apigw.HttpApi(self, "Http", api_name="choir-solfa", create_default_stage=True)
        api_target = integrations.HttpLambdaIntegration("ApiFn", api_fn)
        signed_in = authorizers.HttpUserPoolAuthorizer("SignedIn", pool, user_pool_clients=[client, dark_client])
        http_api.add_routes(
            path="/api/{proxy+}", methods=[apigw.HttpMethod.GET, apigw.HttpMethod.POST, apigw.HttpMethod.DELETE],
            integration=api_target, authorizer=signed_in)
        # Signing in happens before there is a token to check, so these routes have no authorizer.
        http_api.add_routes(path="/api/auth/{proxy+}", methods=[apigw.HttpMethod.POST], integration=api_target)
        # A ceiling for the whole API (API Gateway answers 429 past it), only to cap cost if it is
        # flooded. Each person's song limits are in the Lambda, so one person can't use up others' songs.
        stage = http_api.default_stage.node.default_child
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=int(settings["api_rate"]), throttling_burst_limit=int(settings["api_rate"]) * 2)

        # ---------- the page ----------
        # Security headers on every response. The page's own scripts are allowed by hash, so a
        # script slipped into the page some other way won't run.
        page_scripts = script_hashes(PAGE.read_text(encoding="utf-8"))
        csp = "; ".join([
            "default-src 'self'",
            "script-src 'self' " + " ".join(page_scripts),
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            # The sign-in service (to swap the sign-in code for tokens) and S3 (uploads, and reading a
            # score's text). Any bucket in the region: naming this one would make the bucket, whose
            # CORS rule names the site, and the site depend on each other.
            f"connect-src 'self' https://*.s3.{self.region}.amazonaws.com",
            "object-src 'none'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'",
        ])
        headers = cloudfront.ResponseHeadersPolicy(
            self, "Headers", comment="Choir Solfa security headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp, override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=False, override=True),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.SAME_ORIGIN, override=True)),
            custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(custom_headers=[
                cloudfront.ResponseCustomHeader(header="Permissions-Policy", override=True,
                                                value="camera=(), microphone=(), geolocation=(), payment=()")]))
        # The page's behaviour was made with the distribution, before these settings existed.
        distribution.node.default_child.add_property_override(
            "DistributionConfig.DefaultCacheBehavior.ResponseHeadersPolicyId", headers.response_headers_policy_id)
        api_host = Fn.select(2, Fn.split("/", http_api.api_endpoint))
        distribution.add_behavior(
            "/api/*", origins.HttpOrigin(api_host, protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            response_headers_policy=headers)

        config = {"api": "", "authDomain": (auth_domain or login).base_url(), "clientId": client.user_pool_client_id,
                  "clientIdDark": dark_client.user_pool_client_id,
                  "keepDays": keep_days, "dailyLimit": int(settings["daily_limit"]),
                  "maxMinutes": int(settings["max_song_minutes"]), "maxUploadMb": int(settings["max_upload_mb"])}
        page = PAGE.read_text(encoding="utf-8").replace("__CLOUD__", json.dumps(config))
        s3deploy.BucketDeployment(
            self, "Page", destination_bucket=site_bucket, distribution=distribution, distribution_paths=["/*"],
            sources=[s3deploy.Source.data("index.html", page),
                     s3deploy.Source.data("privacy.html", PRIVACY.read_text(encoding="utf-8")),
                     s3deploy.Source.asset(str(ICON.parent), exclude=["*", "!icon.png", "!logo.svg", "!apple-touch-icon.png", "!favicon.ico"])],
            cache_control=[s3deploy.CacheControl.from_string("no-cache")], memory_limit=256)

        # ---------- an email if spending passes the alert ----------
        # Both when a month's spending is forecast to pass alert_usd and when it actually does.
        # app.py refuses to deploy without alert_email (unless it is "none"), so a deploy from a
        # fresh copy can't quietly remove this.
        if settings.get("alert_email") and settings["alert_email"] != "none":
            subscriber = budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=settings["alert_email"])
            kinds = ("FORECASTED", "ACTUAL")
            # AWS can't change a budget's alerts in place, so CloudFormation makes a new budget and
            # then deletes the old one. A name that changes with the alerts keeps the two apart.
            alerts = hashlib.sha256(json.dumps([settings["alert_email"], kinds]).encode()).hexdigest()[:6]
            budgets.CfnBudget(self, "Budget", budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST", time_unit="MONTHLY", budget_name=f"choir-solfa-{alerts}",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=float(settings["alert_usd"]), unit="USD")),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN", notification_type=kind, threshold=100,
                            threshold_type="PERCENTAGE"),
                        subscribers=[subscriber])
                    for kind in kinds])

        CfnOutput(self, "SiteUrl", value=f"https://{domain or distribution.distribution_domain_name}/")
        CfnOutput(self, "CloudFrontDomain", value=distribution.distribution_domain_name)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "PageConfig", value=Fn.to_json_string(config))
