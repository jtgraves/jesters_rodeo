from aws_cdk import Stack
from constructs import Construct


class JestersRodeoStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Resources added in later tasks: DynamoDB tables, Lambda, API Gateway,
        # Cognito User Pool, SES identity, EventBridge cleanup rule.
