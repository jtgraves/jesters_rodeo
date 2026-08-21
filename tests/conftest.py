import os

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_dummy")
os.environ.setdefault("STRIPE_PUBLISHABLE_KEY", "pk_test_dummy")
os.environ.setdefault("EVENTS_TABLE", "Events")
os.environ.setdefault("ORDERS_TABLE", "Orders")
os.environ.setdefault("TICKETS_TABLE", "Tickets")
os.environ.setdefault("DISCOUNT_CODES_TABLE", "DiscountCodes")
os.environ.setdefault("WAITLIST_TABLE", "Waitlist")
os.environ.setdefault("SES_SENDER_EMAIL", "noreply@example.com")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-1_dummy")
os.environ.setdefault("COGNITO_APP_CLIENT_ID", "dummy_client_id")
os.environ.setdefault("COGNITO_DOMAIN", "jr-admin-test.auth.us-east-1.amazoncognito.com")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("BASE_URL", "http://testserver")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def dynamodb_tables():
    from app import db

    db.reset_clients()
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="Events",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="Orders",
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "order_id", "AttributeType": "S"},
                {"AttributeName": "event_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "event_id-index",
                "KeySchema": [{"AttributeName": "event_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="Tickets",
            KeySchema=[{"AttributeName": "ticket_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "ticket_id", "AttributeType": "S"},
                {"AttributeName": "order_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "order_id-index",
                "KeySchema": [{"AttributeName": "order_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="DiscountCodes",
            KeySchema=[{"AttributeName": "code", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "code", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="Waitlist",
            KeySchema=[{"AttributeName": "waitlist_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "waitlist_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield
    db.reset_clients()
