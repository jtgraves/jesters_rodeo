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
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_ses as ses,
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

        ses.EmailIdentity(
            self, "SesSenderIdentity", identity=ses.Identity.email(sender_email)
        )

        common_env = {
            "EVENTS_TABLE": tables["events"].table_name,
            "ORDERS_TABLE": tables["orders"].table_name,
            "TICKETS_TABLE": tables["tickets"].table_name,
            "DISCOUNT_CODES_TABLE": tables["discount_codes"].table_name,
            "WAITLIST_TABLE": tables["waitlist"].table_name,
            "SES_SENDER_EMAIL": sender_email,
            "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "COGNITO_APP_CLIENT_ID": user_pool_client.user_pool_client_id,
            "COGNITO_DOMAIN": f"{cognito_domain_prefix}.auth.{self.region}.amazoncognito.com",
            "BASE_URL": site_url,
            # NOTE: no AWS_REGION here. It is a reserved Lambda environment
            # variable — setting it fails the deployment outright — and Lambda
            # populates it for us.
        }

        secure_param_prefix = f"/jesters-rodeo/{self.stack_name}"
        common_env["SECURE_PARAM_PREFIX"] = secure_param_prefix

        app_lambda = _lambda.Function(
            self, "AppFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="app.main.handler",
            code=self._bundled_code(),
            timeout=Duration.seconds(15),
            memory_size=512,
            environment=common_env,
        )
        cleanup_lambda = _lambda.Function(
            self, "CleanupFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="scripts.cleanup_pending_orders.handler",
            code=self._bundled_code(),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment=common_env,
        )

        for table in tables.values():
            table.grant_read_write_data(app_lambda)
        tables["orders"].grant_read_write_data(cleanup_lambda)
        tables["events"].grant_read_write_data(cleanup_lambda)

        app_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendRawEmail"],
                resources=["*"],
                conditions={"StringEquals": {"ses:FromAddress": sender_email}},
            )
        )

        for function in (app_lambda, cleanup_lambda):
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

        if domain_name and hosted_zone_id:
            self._create_custom_domain(http_api, domain_name, hosted_zone_id)

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
        discount_codes_table = dynamodb.Table(
            self, "DiscountCodesTable",
            partition_key=dynamodb.Attribute(name="code", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        waitlist_table = dynamodb.Table(
            self, "WaitlistTable",
            partition_key=dynamodb.Attribute(name="waitlist_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        return {
            "events": events_table,
            "orders": orders_table,
            "tickets": tickets_table,
            "discount_codes": discount_codes_table,
            "waitlist": waitlist_table,
        }

    def _create_auth(self, domain_prefix: str, site_url: str):
        user_pool = cognito.UserPool(
            self, "AdminUserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(min_length=12),
            removal_policy=RemovalPolicy.RETAIN,
        )
        user_pool_domain = user_pool.add_domain(
            "AdminHostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )
        # generate_secret=False: the app uses the authorization-code flow with
        # PKCE (Task 8), so there is no client secret to smuggle into a Lambda
        # environment variable and therefore none to leak via CloudFormation.
        user_pool_client = user_pool.add_client(
            "AdminUserPoolClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{site_url}/admin/callback"],
                logout_urls=[f"{site_url}/"],
            ),
        )
        return user_pool, user_pool_client, user_pool_domain

    def _create_custom_domain(self, http_api, domain_name: str, hosted_zone_id: str) -> None:
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self, "HostedZone",
            hosted_zone_id=hosted_zone_id,
            zone_name=".".join(domain_name.split(".")[-2:]),
        )
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
