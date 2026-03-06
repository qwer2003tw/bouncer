# Sprint 11-012: Tasks — deploy_frontend Phase B integration test

> Generated: 2026-03-04

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | test_mcp_deploy_frontend_phase_b.py (1 test file) |
| D2 Cross-module | 0 | 純測試，無 production code 變更 |
| D3 Testing | 3 | 新增 ~10 個整合測試案例 |
| D4 Infrastructure | 0 | 無 |
| D5 External | 0 | 無 |
| **Total TCS** | **4** | ✅ 不需拆分 |

## Task List

```
[012-T1] [P0] [US-1] TestCommandConstruction: S3 copy command 格式驗證（bucket, key, content-type, cache-control, metadata-directive）
[012-T2] [P0] [US-1] TestCommandConstruction: CF invalidation command 格式驗證（distribution-id, paths, region）
[012-T3] [P0] [US-1] TestCommandConstruction: index.html cache-control = no-cache
[012-T4] [P0] [US-1] TestCommandConstruction: assets/* cache-control = immutable
[012-T5] [P1] [US-2] TestFullApproveFlow: 7 files → progress update at file 5 + final
[012-T6] [P1] [US-2] TestFullApproveFlow: DDB 所有 deploy fields 正確寫入
[012-T7] [P1] [US-2] TestFullApproveFlow: partial failure (1/7 fails) → partial_deploy + CF still called
[012-T8] [P1] [US-2] TestFullApproveFlow: Telegram final message 格式正確
[012-T9] [P2] 確認新測試與現有測試不衝突（共用 fixtures）
```
