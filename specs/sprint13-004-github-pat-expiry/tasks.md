# Sprint 13-004: Tasks — GitHub PAT Expiry Monitoring

> Generated: 2026-03-05

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 2 | pat_checker.py（新）、deployer.py、notifications.py |
| D2 Cross-module | 1 | pat_checker ↔ deployer ↔ notifications |
| D3 Testing | 2 | mock urllib + mock secretsmanager + deploy 整合 |
| D4 Infrastructure | 0 | 可能需 IAM 權限（視現有權限而定） |
| D5 External | 0 | GitHub API（public, well-documented） |
| **Total TCS** | **5** | ✅ 不需拆分 |

## Task List

### Core

```
[004-T1] [P0] [US-1] 新建 src/pat_checker.py: check_github_pat(pat) → dict（valid, expires_at, days_remaining）
[004-T2] [P0] [US-1] pat_checker.py: 處理 401 (expired)、timeout (graceful)、no-expiry header (classic PAT)
[004-T3] [P0] [US-2] deployer.py: start_deploy() 開頭加 PAT 檢查邏輯
[004-T4] [P0] [US-2] deployer.py: _get_pat_from_secrets_manager() helper
[004-T5] [P0] [US-1] notifications.py: send_pat_expiry_notification() — expired / expiring 兩種模式
```

### IAM（如需）

```
[004-T6] [P1] 確認主 Bouncer Lambda IAM role 是否有 secretsmanager:GetSecretValue for sam-deployer/*
[004-T7] [P1] 如缺權限 → template.yaml 加 IAM statement（需注意 scope lock — 只加到 spec，不改 template.yaml）
```

### 測試

```
[004-T8]  [P0] 測試: check_github_pat() — 200 OK + expiry header → valid=True, days_remaining 正確
[004-T9]  [P0] 測試: check_github_pat() — 200 OK 無 expiry header → valid=True, days_remaining=None
[004-T10] [P0] 測試: check_github_pat() — 401 → valid=False
[004-T11] [P1] 測試: check_github_pat() — timeout → return None
[004-T12] [P1] 測試: start_deploy — PAT expired → error return, SFN 不啟動
[004-T13] [P1] 測試: start_deploy — PAT expiring (≤7 days) → 通知 + SFN 正常啟動
[004-T14] [P1] 測試: start_deploy — PAT OK → 無通知, SFN 啟動
[004-T15] [P2] 測試: start_deploy — API failure → 跳過, SFN 啟動
[004-T16] [P2] 測試: send_pat_expiry_notification() — expired + expiring 兩種模式 text/entities 驗證
```

## Execution Order

```
T1-T2 → T4 → T3 → T5 → T6-T7 → T8-T16
```
