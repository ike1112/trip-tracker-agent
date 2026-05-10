"""
Test fixtures for the travel-agent Lambda.

The watches module reads its table names from env vars at import time and
constructs boto3 resources eagerly. Tests need:
  1. The env vars set BEFORE watches is imported.
  2. moto's mock_aws active BEFORE the boto3 resource is created.
  3. The actual DynamoDB tables created in the mock account.

The fixture below handles all three by creating tables under mock_aws and
re-importing watches inside the mock context. Tests then receive a fresh
watches module bound to the mocked tables.
"""

import importlib
import os
import sys

import boto3
import pytest
from moto import mock_aws

WATCHES_TABLE = "TestWatches"
FARE_HISTORY_TABLE = "TestFareHistory"


@pytest.fixture
def watches_module():
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["WATCHES_TABLE_NAME"] = WATCHES_TABLE
    os.environ["FARE_HISTORY_TABLE_NAME"] = FARE_HISTORY_TABLE

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=WATCHES_TABLE,
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "watchId", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "watchId", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=FARE_HISTORY_TABLE,
            KeySchema=[
                {"AttributeName": "watchId", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "watchId", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Force a fresh import so the module-level boto3 resource binds
        # to the mocked DynamoDB inside this fixture's context.
        sys.modules.pop("watches", None)
        watches = importlib.import_module("watches")
        yield watches
        sys.modules.pop("watches", None)
