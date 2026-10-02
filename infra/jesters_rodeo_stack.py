from aws_cdk import (
    AssetHashType,
    BundlingOptions,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_s3 as s3,
)
from constructs import Construct

# Everything the deployment artifact does not need. Without this the asset
# also carries .git, the local virtualenv, and the plan document.
ASSET_EXCLUDES = [
    ".git", ".github", ".venv", "venv", "infra", "tests", "docs",
    "*.md", "__pycache__", "*.pyc", ".pytest_cache", ".DS_Store", "cdk.out",
]


class JestersRodeoStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        site_url = self._require_context("site_url")
        cognito_domain_prefix = self._require_context("cognito_domain_prefix")
        sender_email = self._require_context("ses_sender_email")
        domain_name = self.node.try_get_context("domain_name")
        hosted_zone_id = self.node.try_get_context("hosted_zone_id")

        tables = self._create_tables()
        user_pool, user_pool_client, user_pool_domain = self._create_auth(
            cognito_domain_prefix, site_url
        )

        # Mail no longer goes through SES at all (see app/emails.py -- it's
        # Gmail SMTP now, authenticated with smtp_password, an SSM secret
        # alongside the Stripe keys/session_secret below). This zone lookup
        # exists purely for _create_custom_domain's ACM/Route53 setup now,
        # not for any email identity.
        hosted_zone = None
        if domain_name and hosted_zone_id:
            hosted_zone = route53.PublicHostedZone.from_public_hosted_zone_attributes(
                self, "HostedZone",
                hosted_zone_id=hosted_zone_id,
                zone_name=".".join(domain_name.split(".")[-2:]),
            )

        common_env = {
            "EVENTS_TABLE": tables["events"].table_name,
            "ORDERS_TABLE": tables["orders"].table_name,
            "TICKETS_TABLE": tables["tickets"].table_name,
            "WAITLIST_TABLE": tables["waitlist"].table_name,
            "ANNOUNCEMENTS_TABLE": tables["announcements"].table_name,
            "PAST_BENEFICIARIES_TABLE": tables["past_beneficiaries"].table_name,
            "FAQ_ENTRIES_TABLE": tables["faq_entries"].table_name,
            "CLOWN_PROFILES_TABLE": tables["clown_profiles"].table_name,
            "KREWE_LINKS_TABLE": tables["krewe_links"].table_name,
            "SES_SENDER_EMAIL": sender_email,
            "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "COGNITO_APP_CLIENT_ID": user_pool_client.user_pool_client_id,
            # Derived from user_pool_domain itself, not re-built from the raw
            # cognito_domain_prefix context value -- _create_auth appends
            # "-v2" to the prefix it actually uses (see its comments), and
            # duplicating that suffix logic here would silently drift the
            # moment either copy changed.
            "COGNITO_DOMAIN": f"{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com",
            "BASE_URL": site_url,
            # NOTE: no AWS_REGION here. It is a reserved Lambda environment
            # variable — setting it fails the deployment outright — and Lambda
            # populates it for us.
        }

        secure_param_prefix = f"/jesters-rodeo/{self.stack_name}"
        common_env["SECURE_PARAM_PREFIX"] = secure_param_prefix

        # Admin-uploaded banner/logo images. Public read via a bucket policy
        # (not ACLs, which S3 discourages/blocks by default on new buckets) --
        # these images are rendered on the public registration page, so they
        # have to be reachable by a plain, permanent URL. A private bucket
        # would mean presigned URLs, which expire and would silently break
        # the page days later.
        images_bucket = s3.Bucket(
            self, "EventImagesBucket",
            block_public_access=s3.BlockPublicAccess(
                block_public_acls=True,
                ignore_public_acls=True,
                block_public_policy=False,
                restrict_public_buckets=False,
            ),
            public_read_access=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # INFO-level, JSON-formatted logs. At 100-500 tickets/year the volume
        # is negligible, and mangum's request-level "METHOD path status" line
        # is what makes a stuck checkout/webhook diagnosable at all from
        # CloudWatch after the fact.
        #
        # log_retention: without it, Lambda's auto-created log group keeps
        # everything forever -- the one cost in this stack with no ceiling,
        # since every other resource here is billed by actual usage. 90 days
        # is well past what a stuck-checkout investigation ever needs. (No
        # companion removal-policy prop here -- log_retention provisions the
        # log group via CDK's own custom resource, which doesn't expose one;
        # the log group is disposable operational exhaust either way.)
        log_settings = dict(
            logging_format=_lambda.LoggingFormat.JSON,
            application_log_level_v2=_lambda.ApplicationLogLevel.INFO,
            system_log_level_v2=_lambda.SystemLogLevel.INFO,
            log_retention=logs.RetentionDays.THREE_MONTHS,
        )

        # Unlike before: this function now DOES need common_env as-is
        # (including SECURE_PARAM_PREFIX), since sending mail over SMTP
        # (app/emails.py) means it needs smtp_password from SSM -- the
        # SSM/KMS grant loop below now includes this function too.
        announcement_env = dict(common_env)

        # Built before app_lambda: app_lambda's environment needs
        # announcement_lambda.function_name inline, so the Python object must
        # already exist. This is a plain name-binding requirement, not a CDK
        # circular-dependency concern -- cross-resource references resolve at
        # synth/deploy time regardless of construct declaration order.
        announcement_lambda = _lambda.Function(
            self, "AnnouncementFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="scripts.send_announcement.handler",
            code=self._bundled_code(),
            # One-by-one SMTP sends; ~500 recipients worst case is well
            # inside this. No pagination/resumability at this scale -- a
            # deliberate limit, not an oversight.
            timeout=Duration.minutes(5),
            memory_size=256,
            environment=announcement_env,
            **log_settings,
        )

        app_lambda = _lambda.Function(
            self, "AppFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="app.main.handler",
            code=self._bundled_code(),
            timeout=Duration.seconds(15),
            memory_size=512,
            environment={
                **common_env,
                "ANNOUNCEMENT_LAMBDA_NAME": announcement_lambda.function_name,
                "EVENT_IMAGES_BUCKET": images_bucket.bucket_name,
            },
            **log_settings,
        )
        cleanup_lambda = _lambda.Function(
            self, "CleanupFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="scripts.cleanup_pending_orders.handler",
            code=self._bundled_code(),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment=common_env,
            **log_settings,
        )

        for table in tables.values():
            table.grant_read_write_data(app_lambda)
        tables["orders"].grant_read_write_data(cleanup_lambda)
        tables["events"].grant_read_write_data(cleanup_lambda)

        tables["announcements"].grant_read_write_data(announcement_lambda)
        tables["orders"].grant_read_data(announcement_lambda)
        tables["waitlist"].grant_read_data(announcement_lambda)

        announcement_lambda.grant_invoke(app_lambda)
        images_bucket.grant_write(app_lambda)

        # The admin "Clown Management" page manages Cognito users in this pool:
        # every user is a clown (member); the ones in the "admins" group are
        # clowns with full admin privileges.
        cognito.CfnUserPoolGroup(
            self, "AdminsGroupV2",
            user_pool_id=user_pool.user_pool_id,
            group_name="admins",
            description="Clowns with full admin privileges",
        )
        app_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:ListUsers",
                    "cognito-idp:ListUsersInGroup",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminDeleteUser",
                    "cognito-idp:AdminAddUserToGroup",
                    "cognito-idp:AdminRemoveUserFromGroup",
                ],
                resources=[user_pool.user_pool_arn],
            )
        )

        # announcement_lambda included now too: it sends mail over SMTP
        # (app/emails.py) and needs smtp_password from SSM just like
        # app_lambda/cleanup_lambda already do.
        for function in (app_lambda, cleanup_lambda, announcement_lambda):
            function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["ssm:GetParameters", "ssm:GetParameter"],
                    resources=[
                        f"arn:aws:ssm:{self.region}:{self.account}:parameter"
                        f"{secure_param_prefix}/*"
                    ],
                )
            )
            function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["kms:Decrypt"],
                    resources=["*"],
                    conditions={
                        "StringEquals": {"kms:ViaService": f"ssm.{self.region}.amazonaws.com"}
                    },
                )
            )

        CfnOutput(self, "SecureParamPrefix", value=secure_param_prefix)

        http_api = apigwv2.HttpApi(
            self, "HttpApi",
            default_integration=apigwv2_integrations.HttpLambdaIntegration(
                "AppIntegration", app_lambda
            ),
        )

        # Every 15 minutes: a backstop behind the checkout.session.expired
        # webhook, so an unclaimed seat is never held for more than an hour.
        rule = events.Rule(
            self, "CleanupScheduleRule",
            schedule=events.Schedule.rate(Duration.minutes(15)),
        )
        rule.add_target(targets.LambdaFunction(cleanup_lambda))

        if hosted_zone is not None:
            self._create_custom_domain(http_api, domain_name, hosted_zone)

        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "SiteUrl", value=site_url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "CognitoDomain", value=user_pool_domain.base_url())

    def _require_context(self, key: str) -> str:
        value = self.node.try_get_context(key)
        if not value:
            raise ValueError(
                f"Missing required CDK context '{key}'. "
                f"Pass it with -c {key}=... or add it to infra/cdk.json."
            )
        return value

    def _bundled_code(self) -> _lambda.Code:
        """Package app/ and scripts/ together with their dependencies.

        The path is ".." because the CDK app runs from infra/. Bundling runs
        pip inside the official Python 3.12 Lambda image, so Docker must be
        running for `cdk deploy`.

        The Lambda functions run on x86_64, but on an Apple Silicon host the
        bundling container runs arm64 (a ``platform="linux/amd64"`` hint is
        silently ignored when Docker has no working amd64 emulation). So rather
        than trust the container architecture, pip is told explicitly to
        resolve linux x86_64 wheels: ``--platform`` + the mandatory
        ``--only-binary=:all:``. Without this, arm64 .so files land in an
        x86_64 Lambda and every invocation fails at import with
        "No module named 'pydantic_core._pydantic_core'".
        """
        return _lambda.Code.from_asset(
            "..",
            exclude=ASSET_EXCLUDES,
            # Hash from the bundled OUTPUT, not just source contents: the app/
            # sources can be unchanged while the bundle must be rebuilt (a
            # dependency bump in requirements.txt, or the pip flags below).
            # With the default SOURCE hash, CDK reuses a stale cached bundle.
            asset_hash_type=AssetHashType.OUTPUT,
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                command=[
                    "bash", "-c",
                    "pip install --no-cache-dir -r requirements.txt -t /asset-output "
                    "--platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 "
                    "--implementation cp --python-version 3.12 --only-binary=:all: "
                    "&& cp -r app scripts /asset-output/",
                ],
            ),
        )

    def _create_tables(self) -> dict:
        events_table = dynamodb.Table(
            self, "EventsTable",
            partition_key=dynamodb.Attribute(name="event_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        orders_table = dynamodb.Table(
            self, "OrdersTable",
            partition_key=dynamodb.Attribute(name="order_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        orders_table.add_global_secondary_index(
            index_name="event_id-index",
            partition_key=dynamodb.Attribute(name="event_id", type=dynamodb.AttributeType.STRING),
        )
        tickets_table = dynamodb.Table(
            self, "TicketsTable",
            partition_key=dynamodb.Attribute(name="ticket_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        tickets_table.add_global_secondary_index(
            index_name="order_id-index",
            partition_key=dynamodb.Attribute(name="order_id", type=dynamodb.AttributeType.STRING),
        )
        waitlist_table = dynamodb.Table(
            self, "WaitlistTable",
            partition_key=dynamodb.Attribute(name="waitlist_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        announcements_table = dynamodb.Table(
            self, "AnnouncementsTable",
            partition_key=dynamodb.Attribute(name="announcement_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        past_beneficiaries_table = dynamodb.Table(
            self, "PastBeneficiariesTable",
            partition_key=dynamodb.Attribute(
                name="beneficiary_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        faq_entries_table = dynamodb.Table(
            self, "FaqEntriesTable",
            partition_key=dynamodb.Attribute(name="faq_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        clown_profiles_table = dynamodb.Table(
            self, "ClownProfilesTable",
            partition_key=dynamodb.Attribute(name="clown_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        krewe_links_table = dynamodb.Table(
            self, "KreweLinksTable",
            partition_key=dynamodb.Attribute(name="link_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        return {
            "events": events_table,
            "orders": orders_table,
            "tickets": tickets_table,
            "waitlist": waitlist_table,
            "announcements": announcements_table,
            "past_beneficiaries": past_beneficiaries_table,
            "faq_entries": faq_entries_table,
            "clown_profiles": clown_profiles_table,
            "krewe_links": krewe_links_table,
        }

    def _create_auth(self, domain_prefix: str, site_url: str):
        # AdminUserPoolV2 / -V2 suffixes throughout: Cognito flatly refuses to
        # change sign_in_case_sensitive on an existing pool (confirmed against
        # AWS's own docs -- "you can't change ... from case-sensitive to
        # case-insensitive. Instead, migrate your users to a new user pool"),
        # and even if it didn't, CloudFormation's own metadata for that
        # property is misleadingly optimistic ("Update requires: No
        # interruption"), which would make it attempt an in-place update that
        # the Cognito API would then reject -- a failed update on a live
        # stack. New logical IDs force CDK to CREATE a fresh pool instead of
        # trying to mutate the old one. The old pool (AdminUserPool, no
        # longer referenced anywhere in this file) is RemovalPolicy.RETAIN,
        # so it survives, orphaned, as a fallback until manually deleted --
        # its 2 existing users need to be recreated in this new pool.
        user_pool = cognito.UserPool(
            self, "AdminUserPoolV2",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            sign_in_case_sensitive=False,
            password_policy=cognito.PasswordPolicy(min_length=12),
            # Was REQUIRED (TOTP) -- turned off at the user's request. Users
            # who had already completed TOTP enrollment stop being
            # challenged for it too, not just new sign-ins: MfaConfiguration
            # OFF disables MFA pool-wide, for everyone, not per-user. A pure
            # in-place update (CloudFormation: "Update requires: No
            # interruption") -- no new pool, no re-invite, unlike the
            # sign_in_case_sensitive migration above. mfa_second_factor is
            # dropped too: it's ignored by CDK whenever mfa=OFF.
            mfa=cognito.Mfa.OFF,
            # Unset, this is Cognito's bare default: "Your username is
            # {username} and temporary password is {####}." -- no branding,
            # no context on what this is, no link. {####} is Cognito's
            # required placeholder for the generated temporary password;
            # {username} turned out to be required too -- not just optional
            # as CDK's own doc comment on UserInvitationConfig claims --
            # confirmed by a real deploy failure ("Invalid Email message
            # body for Admin create user flow parameter. Email message body
            # should have {username} which will be replaced by code").
            # {username} on this pool is the opaque sub (see _list_clowns in
            # app/routes/admin.py), not the clown's email, so it's included
            # but de-emphasized as a reference code rather than the sign-in
            # instruction -- the real instruction is to use the email this
            # arrived at. This same template is reused for both the initial
            # invite and "Resend invite" (admin_create_user
            # MessageAction="RESEND" in app/routes/admin.py). Plain text
            # only -- Cognito's own built-in email sending doesn't render
            # HTML/images, so there's no logo here the way the SMTP-sent
            # ticket confirmation email has one.
            user_invitation=cognito.UserInvitationConfig(
                email_subject="You're invited to join Jester's Reaux-de-Eaux!",
                email_body=(
                    "Howdy! You've been invited to join the members-only Clowns "
                    "section for Jester's Reaux-de-Eaux, our Mardi Gras krewe.\n\n"
                    f"Sign in at {site_url}/admin/login using the email address "
                    "this invitation was sent to, with this temporary password "
                    "-- you'll be asked to set your own on first sign-in:\n\n"
                    "{####}\n\n"
                    "(Account reference: {username})\n\n"
                    "See you on the float!\n"
                    "-- Jester's Reaux-de-Eaux"
                ),
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        # A new prefix, not the original: Cognito domain prefixes are
        # globally unique, so a same-prefix domain on the new pool can't be
        # created while the old one (still attached to the old, retained
        # pool) exists -- and UserPoolDomain's Domain/UserPoolId properties
        # both require CloudFormation replacement, so reusing the prefix
        # would mean gambling on delete-then-create replacement ordering
        # against a live stack. This prefix is an internal OAuth redirect
        # target the app builds itself (COGNITO_DOMAIN); nobody bookmarks or
        # types it, so the rename is invisible to end users.
        user_pool_domain = user_pool.add_domain(
            "AdminHostedUiDomainV2",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"{domain_prefix}-v2"),
        )
        # generate_secret=False: the app uses the authorization-code flow with
        # PKCE (Task 8), so there is no client secret to smuggle into a Lambda
        # environment variable and therefore none to leak via CloudFormation.
        user_pool_client = user_pool.add_client(
            "AdminUserPoolClientV2",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{site_url}/admin/callback"],
                logout_urls=[f"{site_url}/"],
            ),
            # This app only ever holds the ID token -- no refresh flow -- and
            # verify_cognito_token enforces its exp claim on every request, so
            # Cognito's default 60-minute id_token_validity is the real ceiling
            # on a session regardless of app.auth.SESSION_MAX_AGE_SECONDS.
            # Match the two so the cookie's stated lifetime is the real one.
            id_token_validity=Duration.hours(8),
        )
        return user_pool, user_pool_client, user_pool_domain

    def _create_custom_domain(self, http_api, domain_name: str, zone: route53.IHostedZone) -> None:
        # zone is the same PublicHostedZone reference imported once in
        # __init__ -- importing it a second time under a second logical id
        # would just be redundant.
        certificate = acm.Certificate(
            self, "SiteCertificate",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(zone),
        )
        api_domain = apigwv2.DomainName(
            self, "ApiDomainName", domain_name=domain_name, certificate=certificate
        )
        apigwv2.ApiMapping(self, "ApiMapping", api=http_api, domain_name=api_domain)
        route53.ARecord(
            self, "ApiAliasRecord",
            zone=zone,
            record_name=domain_name,
            target=route53.RecordTarget.from_alias(
                route53_targets.ApiGatewayv2DomainProperties(
                    api_domain.regional_domain_name, api_domain.regional_hosted_zone_id
                )
            ),
        )
