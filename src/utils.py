"""
Bouncer - 工具函數模組
"""

import hashlib
import json
import time
from decimal import Decimal
from typing import Optional

from constants import AUDIT_TTL_SHORT, AUDIT_TTL_LONG


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
            'Content-Type': 'application/json'
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


def log_decision(table, request_id, command, reason, source, account_id,
                 decision_type, risk_score=None, risk_factors=None,
                 sequence_modifier=None, **kwargs):
    """統一的決策記錄函數 — 記錄所有審批決策到 requests 表"""
    now = int(time.time())
    item = {
        'request_id': request_id,
        'command': command[:2000],
        'reason': reason[:500],
        'source': source or '__anonymous__',
        'account_id': account_id or '',
        'decision_type': decision_type,
        'status': decision_type,  # 向後兼容
        'created_at': now,
        'decided_at': now,
        'decision_latency_ms': 0,
        'ttl': now + AUDIT_TTL_LONG,  # 90 天保留（blocked/compliance 30 天）
    }
    if decision_type in ('blocked', 'compliance_violation'):
        item['ttl'] = now + AUDIT_TTL_SHORT  # 30 天
    if risk_score is not None:
        item['risk_score'] = Decimal(str(risk_score))
        item['risk_category'] = kwargs.pop('risk_category', '')
    if risk_factors:
        item['risk_factors'] = risk_factors[:5]
    if sequence_modifier is not None:
        item['sequence_modifier'] = str(sequence_modifier)
    item.update({k: v for k, v in kwargs.items() if v is not None})
    try:
        table.put_item(Item=item)
    except Exception as e:
        print(f"[AUDIT] Failed to log decision: {e}")
    return item
