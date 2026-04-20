"""
Bouncer - Execute Helpers

Utility functions for execute pipeline.
"""

import json
import secrets
import time
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from constants import AUDIT_TTL_SHORT, SHADOW_TABLE_NAME

logger = Logger(service="bouncer")


def _safe_risk_category(smart_decision):
    """安全取得 risk category 值（相容 enum 和 string）"""
    if not smart_decision:
        return None
    try:
        cat = smart_decision.risk_result.category
        return cat.value if hasattr(cat, 'value') else cat
    except (AttributeError, KeyError) as e:
        logger.warning("Failed to extract risk category: %s", e, extra={"src_module": "execute", "operation": "safe_risk_category", "error": str(e)})
        return None


def _safe_risk_factors(smart_decision):
    """安全取得 risk factors（相容各種格式，float → Decimal）"""
    if not smart_decision:
        return None
    try:
        from decimal import Decimal as _Dec
        factors = [f.__dict__ for f in smart_decision.risk_result.factors[:5]]
        # 將 float 轉為 Decimal（DynamoDB 不接受 float）
        sanitized = []
        for factor in factors:
            sanitized.append({
                k: _Dec(str(v)) if isinstance(v, float) else v
                for k, v in factor.items()
            })
        return sanitized
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.warning("Failed to extract/convert risk factors: %s", e, extra={"src_module": "execute", "operation": "safe_risk_factors", "error": str(e)})
        return None


def _log_smart_approval_shadow(
    req_id: str,
    command: str,
    reason: str,
    source: str,
    account_id: str,
    smart_decision,
    actual_decision: str = '',
) -> None:
    """
    記錄智慧審批決策到 DynamoDB（Shadow Mode）
    用於收集數據，評估準確率後再啟用
    """
    import boto3 as boto3_shadow  # 避免與頂層 import 衝突
    try:
        dynamodb = boto3_shadow.resource('dynamodb')
        table = dynamodb.Table(SHADOW_TABLE_NAME)

        shadow_id = f"shadow-{secrets.token_hex(12)}"
        item = {
            'request_id': shadow_id,
            'mcp_req_id': req_id,
            'timestamp': int(time.time()),
            'command': command[:500],
            'reason': reason[:200],
            'source': source or 'unknown',
            'account_id': account_id,
            'smart_decision': smart_decision.decision,
            'smart_score': smart_decision.final_score,
            'smart_category': smart_decision.risk_result.category.value,
            'smart_factors': json.dumps([f.__dict__ for f in smart_decision.risk_result.factors[:5]], default=str),
            'actual_decision': actual_decision,
            'ttl': int(time.time()) + AUDIT_TTL_SHORT,
        }

        table.put_item(Item=item)
        logger.info("Shadow logged: %s -> %s (score=%s, actual=%s)", shadow_id, smart_decision.decision, smart_decision.final_score, actual_decision, extra={"src_module": "shadow", "operation": "log_shadow", "shadow_id": shadow_id, "decision": smart_decision.decision})
    except ClientError as e:
        # Shadow 記錄失敗不影響主流程
        logger.exception("Shadow log failed: %s", e, extra={"src_module": "shadow", "operation": "log_shadow", "error": str(e)})


def _extract_actual_decision(result: dict) -> str:
    """Extract actual decision from pipeline result for shadow comparison.

    Pipeline returns: {'statusCode': 200, 'body': '{"jsonrpc":"2.0","result":{"content":[{"type":"text","text":"{\\"status\\":\\"auto_approved\\",...}"}]}}'}
    """
    try:
        body = result.get('body', '{}')
        if isinstance(body, str):
            body = json.loads(body)
        # MCP result path
        content = body.get('result', {}).get('content', [])
        if not content:
            # MCP error path
            if 'error' in body:
                return 'error'
            # REST path (body is the response directly)
            status = body.get('status', '')
            if status:
                return _map_status_to_decision(status)
            return 'unknown'
        text = content[0].get('text', '{}')
        data = json.loads(text)
        status = data.get('status', '')
        return _map_status_to_decision(status)
    except (json.JSONDecodeError, KeyError, TypeError, IndexError) as e:
        logger.warning("Failed to parse response decision: %s", e, extra={"src_module": "execute", "operation": "extract_decision", "error": str(e)})
        return 'unknown'


def _map_status_to_decision(status: str) -> str:
    """Map pipeline status to comparable decision label."""
    mapping = {
        'auto_approved': 'auto_approve',
        'blocked': 'blocked',
        'compliance_violation': 'blocked',
        'pending_approval': 'needs_approval',
        'trust_auto_approved': 'auto_approve',
        'grant_auto_approved': 'auto_approve',
    }
    return mapping.get(status, status or 'unknown')
