"""
Bouncer - Smart Approval Module
智慧審批整合模組：結合 risk_scorer 和 sequence_analyzer 做智慧決策

這個模組提供統一的 API 給 mcp_tools.py 使用，替換原有的 is_blocked/is_auto_approve 邏輯。
"""

import logging
from typing import Dict


from risk_scorer import calculate_risk, RiskCategory, RiskResult, RiskFactor
from sequence_analyzer import get_sequence_risk_modifier

logger = logging.getLogger(__name__)

__all__ = [
    'evaluate_command',
    'ApprovalDecision',
]


class ApprovalDecision:
    """審批決策結果"""

    # 決策類型
    AUTO_APPROVE = 'auto_approve'      # 自動批准
    AUTO_APPROVE_LOG = 'auto_approve_log'  # 自動批准但記錄
    NEEDS_CONFIRMATION = 'needs_confirmation'  # 需要確認 reason
    NEEDS_APPROVAL = 'needs_approval'  # 需要人工審批
    BLOCKED = 'blocked'                # 直接拒絕

    def __init__(
        self,
        decision: str,
        risk_result: RiskResult,
        sequence_modifier: float = 0.0,
        final_score: int = 0,
        reason: str = '',
    ):
        self.decision = decision
        self.risk_result = risk_result
        self.sequence_modifier = sequence_modifier
        self.final_score = final_score
        self.reason = reason

    def to_dict(self) -> Dict:
        """轉換為 dict 供 JSON 序列化"""
        return {
            'decision': self.decision,
            'final_score': self.final_score,
            'base_score': self.risk_result.score,
            'sequence_modifier': self.sequence_modifier,
            'category': self.risk_result.category.value,
            'factors': [f.__dict__ for f in self.risk_result.factors],
            'recommendation': self.risk_result.recommendation,
            'reason': self.reason,
        }


def evaluate_command(
    command: str,
    reason: str,
    source: str,
    account_id: str,
    enable_sequence_analysis: bool = True,
) -> ApprovalDecision:
    """
    評估命令並返回審批決策

    這是主要的 API，替換原有的 is_blocked/is_auto_approve 邏輯。

    Args:
        command: AWS CLI 命令
        reason: 執行原因
        source: 請求來源 (agent id)
        account_id: 目標 AWS 帳號
        enable_sequence_analysis: 是否啟用序列分析

    Returns:
        ApprovalDecision: 包含決策和詳細資訊
    """
    try:
        # Step 1: 計算基礎風險分數
        risk_result = calculate_risk(command, reason, source, account_id)
        base_score = risk_result.score

        # Step 2: 序列分析修正（可選）
        sequence_modifier = 0.0
        if enable_sequence_analysis:
            try:
                sequence_modifier, _seq_reason = get_sequence_risk_modifier(source, command)
            except Exception as e:
                logger.warning(f"Sequence analysis failed: {e}")
                # 序列分析失敗不影響主流程

        # Step 3: 計算最終分數（加入序列修正）
        # sequence_modifier 是 -0.3 到 +0.3 的修正值
        final_score = int(base_score * (1 + sequence_modifier))
        final_score = max(0, min(100, final_score))  # 確保在 0-100

        # Step 4: 根據分數決定審批流程
        if risk_result.category == RiskCategory.BLOCK or final_score >= 86:
            decision = ApprovalDecision.BLOCKED
            reason_text = f"風險分數 {final_score} >= 86，自動拒絕"
        elif final_score <= 25:
            decision = ApprovalDecision.AUTO_APPROVE
            reason_text = f"風險分數 {final_score} <= 25，自動批准"
        elif final_score <= 45:
            decision = ApprovalDecision.NEEDS_CONFIRMATION
            reason_text = f"風險分數 {final_score}，需要確認（中等風險）"
        elif final_score <= 65:
            decision = ApprovalDecision.NEEDS_CONFIRMATION
            reason_text = f"風險分數 {final_score}，需要確認 reason"
        else:
            decision = ApprovalDecision.NEEDS_APPROVAL
            reason_text = f"風險分數 {final_score}，需要人工審批"

        return ApprovalDecision(
            decision=decision,
            risk_result=risk_result,
            sequence_modifier=sequence_modifier,
            final_score=final_score,
            reason=reason_text,
        )

    except Exception as e:
        # Fail-closed: 任何錯誤都 fallback 到人工審批
        logger.error(f"Risk evaluation failed: {e}")
        fallback_result = RiskResult(
            score=70,
            category=RiskCategory.MANUAL,
            factors=[RiskFactor(
                name="evaluation_error",
                category="error",
                raw_score=70,
                weighted_score=70,
                weight=1.0,
                details=str(e)
            )],
            recommendation="評估失敗，需要人工審批",
            parsed_command=None,
        )
        return ApprovalDecision(
            decision=ApprovalDecision.NEEDS_APPROVAL,
            risk_result=fallback_result,
            sequence_modifier=0.0,
            final_score=70,
            reason=f"風險評估失敗: {e}",
        )

