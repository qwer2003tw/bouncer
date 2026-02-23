"""DynamoDB table references — single source of truth.

Import from here instead of app.py to avoid circular dependencies.
"""

import boto3
from constants import TABLE_NAME, ACCOUNTS_TABLE_NAME

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)
accounts_table = dynamodb.Table(ACCOUNTS_TABLE_NAME)
