# Tasks — Sprint 32-001a Changeset Analyzer Core

## TCS Scoring Guide
D1 Files (0-5) + D2 Cross-module (0-5) + D3 Testing (0-5) + D4 Infra (0-5) + D5 External (0-5)
Simple: 0-6 | Medium: 7-12 | Complex: 13+ (must split)

---

## Tasks

[T001] [P1] 建立 `src/changeset_analyzer.py`：AnalysisResult dataclass + is_code_only_change() 純函數
| TCS=4 (Simple)
| D1=1（1 新檔）D2=0（無跨模組）D3=2（需白名單邏輯測試）D4=0 D5=1（CFN API schema 理解）
| Deliverable: dataclass + is_code_only_change 函數，可 import，無 AWS 依賴

[T002] [P1] 實作 create_dry_run_changeset(cfn_client, stack_name, template_url) → str
| TCS=5 (Simple)
| D1=1 D2=0 D3=2 D4=0 D5=2（CFN CreateChangeSet API，Capabilities 清單）
| Notes: ChangeSetType=UPDATE，不帶 Parameters（沿用現有 stack 參數），名稱格式 bouncer-dryrun-{uuid[:12]}

[T003] [P1] 實作 analyze_changeset()：poll CREATE_COMPLETE/FAILED，parse ResourceChanges
| TCS=7 (Medium)
| D1=1 D2=0 D3=3（需 mock poll 序列）D4=0 D5=3（DescribeChangeSet response schema，pagination 考量）
| Notes: max_wait=60s, poll_interval=2s；FAILED status reason 寫入 error；timeout 也寫 error

[T004] [P1] 實作 cleanup_changeset()：靜默 DeleteChangeSet，忽略 ChangeSetNotFoundException
| TCS=3 (Simple)
| D1=1 D2=0 D3=1 D4=0 D5=1
| Notes: botocore ClientError code == 'ChangeSetNotFoundException' → pass

[T005] [P1] 新增 `tests/test_changeset_analyzer.py`（8+ test cases）
| TCS=6 (Simple)
| D1=1（1 新測試檔）D2=1（import changeset_analyzer）D3=4（8+ cases，mock cfn_client）D4=0 D5=0
| Test cases:
|   TC01 - 2x Lambda Code Modify → is_code_only == True
|   TC02 - Lambda + DynamoDB Modify → False
|   TC03 - Lambda Action=Add → False
|   TC04 - Lambda Action=Remove → False
|   TC05 - Lambda Timeout (non-Code) property change → False
|   TC06 - AnalysisResult.error != None → is_code_only_change returns False
|   TC07 - empty resource_changes (no-op) → True
|   TC08 - cleanup ChangeSetNotFoundException → no exception raised
|   TC09 - analyze_changeset FAILED status → AnalysisResult.error populated (bonus)
|   TC10 - analyze_changeset timeout → AnalysisResult.error populated (bonus)

---

## Summary

| Task | TCS | Complexity |
|------|-----|------------|
| T001 | 4   | Simple     |
| T002 | 5   | Simple     |
| T003 | 7   | Medium     |
| T004 | 3   | Simple     |
| T005 | 6   | Simple     |
| **Total** | **25** | **（5 tasks，全部 Simple/Medium）** |

---

## Dependencies
- 無需 001b 完成（Phase 1 完全獨立）
- 001b 的 T001 需要本 spec 的所有 tasks 完成後才能整合
