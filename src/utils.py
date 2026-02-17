"""
Bouncer - 工具函數模組
"""

import hashlib
import json
from decimal import Decimal
from typing import Optional


def get_header(headers: dict, key: str) -> Optional[str]:
    """Case-insensitive header lookup for API Gateway compatibility"""
    if headers is None:
        return None
    if key in headers:
        return headers[key]
    lower_key = key.lower()
    if lower_key in headers:
        return headers[lower_key]
    for k, v in headers.items():
        if k.lower() == lower_key:
            return v
    return None


def generate_request_id(command: str) -> str:
    """生成唯一請求 ID"""
    import time
    hash_input = f"{command}:{time.time()}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:12]


def decimal_to_native(obj):
    """將 DynamoDB Decimal 轉換為 Python 原生類型"""
    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decimal_to_native(i) for i in obj]
    return obj


def response(status_code: int, body: dict) -> dict:
    """標準 API 回應格式"""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body, ensure_ascii=False)
    }


def mcp_result(req_id, result: dict) -> dict:
    """MCP JSON-RPC 成功回應"""
    return response(200, {
        'jsonrpc': '2.0',
        'id': req_id,
        'result': result
    })


def mcp_error(req_id, code: int, message: str) -> dict:
    """MCP JSON-RPC 錯誤回應"""
    return response(200, {
        'jsonrpc': '2.0',
        'id': req_id,
        'error': {
            'code': code,
            'message': message
        }
    })
