"""
Bouncer - 帳號管理模組
處理 AWS 帳號的 CRUD 和驗證
"""
import time
import json
import urllib.request
from typing import Optional, Dict

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger
import db as _db

from constants import (
    DEFAULT_ACCOUNT_ID, TELEGRAM_TOKEN
)

logger = Logger(service="bouncer")

__all__ = [
    'init_bot_commands',
    'init_default_account',
    'get_account',
    'list_accounts',
    'validate_account_id',
    'validate_role_arn',
]

# DynamoDB - via db.py (lazy init)
# _accounts_table: test injection shim (monkeypatch sets this directly)
_accounts_table = None


def _get_accounts_table():
    if _accounts_table is not None:
        return _accounts_table
    return _db.accounts_table


# Bot commands 初始化標記（避免每次 invoke 都呼叫 API）
_bot_commands_initialized = False


def init_bot_commands():
    """初始化 Telegram Bot 指令選單（cold start 時執行一次）"""
    global _bot_commands_initialized
    if _bot_commands_initialized or not TELEGRAM_TOKEN:
        return

    commands = [
        {"command": "accounts", "description": "列出 AWS 帳號"},
        {"command": "trust", "description": "列出信任時段"},
        {"command": "pending", "description": "列出待審批請求"},
        {"command": "help", "description": "顯示指令說明"}
    ]

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"commands": commands}).encode(),
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=5)  # nosec B310
        _bot_commands_initialized = True
        logger.info("Bot commands initialized", extra={"src_module": "accounts", "operation": "init_bot_commands"})
    except (OSError, TimeoutError, ConnectionError) as e:
        logger.error("Failed to set bot commands: %s", e, extra={"src_module": "accounts", "operation": "init_bot_commands", "error": str(e)})


def init_default_account():
    """初始化預設帳號（如果不存在）"""
    try:
        result = _get_accounts_table().get_item(Key={'account_id': DEFAULT_ACCOUNT_ID})
        if 'Item' not in result:
            _get_accounts_table().put_item(Item={
                'account_id': DEFAULT_ACCOUNT_ID,
                'name': 'Default',
                'role_arn': None,
                'is_default': True,
                'enabled': True,
                'created_at': int(time.time())
            })
    except ClientError as e:
        logger.error("Error initializing default account: %s", e, extra={"src_module": "accounts", "operation": "init_default_account", "error": str(e)})


def get_account(account_id: str) -> Optional[Dict]:
    """取得帳號配置"""
    try:
        result = _get_accounts_table().get_item(Key={'account_id': account_id})
        return result.get('Item')
    except ClientError as e:
        logger.error("get_account error: %s", e, extra={"src_module": "accounts", "operation": "get_account", "account_id": account_id, "error": str(e)})
        return None


def list_accounts() -> list:
    """列出所有帳號"""
    try:
        result = _get_accounts_table().scan()
        return result.get('Items', [])
    except ClientError as e:
        logger.error("list_accounts error: %s", e, extra={"src_module": "accounts", "operation": "list_accounts", "error": str(e)})
        return []


def validate_account_id(account_id: str) -> tuple:
    """驗證帳號 ID 格式"""
    if not account_id:
        return False, "帳號 ID 不能為空"
    if not account_id.isdigit():
        return False, "帳號 ID 必須是數字"
    if len(account_id) != 12:
        return False, "帳號 ID 必須是 12 位數字"
    return True, None


def validate_role_arn(role_arn: str) -> tuple:
    """驗證 Role ARN 格式"""
    if not role_arn:
        return True, None  # 空的 role_arn 是允許的（預設帳號）
    if not role_arn.startswith('arn:aws:iam::'):
        return False, "Role ARN 格式不正確，應該是 arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME"
    if ':role/' not in role_arn:
        return False, "Role ARN 格式不正確，缺少 :role/"
    return True, None
