# Sprint 9-003: Tasks — bouncer_deploy_frontend + 批次審批

> Generated: 2026-03-02

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 5 | mcp_deploy_frontend.py (new), callbacks.py, tool_schema.py, mcp_tools.py, notifications.py (5+ files) |
| D2 Cross-module | 4 | 新模組 + callbacks 新 action + notifications 新函數（interface change） |
| D3 Testing | 2 | 補測試（新 tool + callback） |
| D4 Infrastructure | 0 | 不改 template.yaml（用 DDB 配置） |
| D5 External | 2 | S3 + CloudFront（已知 AWS service） |
| **Total TCS** | **13** | ⚠️ 剛好 13，建議拆分為 Phase A (tool + staging) 和 Phase B (callback + deploy) |

## Task List

### Phase A: Tool + Staging（可獨立交付）
```
[003-T1] [P1] [US-1] 新增 mcp_deploy_frontend.py：input validation + project config lookup
[003-T2] [P1] [US-1] mcp_deploy_frontend.py：files staging 到 S3（複用 upload_batch staging 邏輯）
[003-T3] [P1] [US-1] mcp_deploy_frontend.py：DDB pending 記錄寫入
[003-T4] [P1] [US-3] notifications.py：send_deploy_frontend_notification()（Telegram UI）
[003-T5] [P1] [US-1] tool_schema.py + mcp_tools.py：註冊 bouncer_deploy_frontend
```

### Phase B: Callback + Deploy（依賴 Phase A）
```
[003-T6] [P1] [US-2] callbacks.py：deploy_frontend approve handler（從 staging → frontend bucket）
[003-T7] [P1] [US-2] callbacks.py：Content-Type + Cache-Control 自動設定
[003-T8] [P1] [US-2] callbacks.py：CloudFront invalidation
[003-T9] [P1] [US-2] callbacks.py：部分 deploy 失敗處理 + response 結構
[003-T10] [P2] [US-3] Telegram 訊息更新（部署進度 → 完成/失敗結果）
```

### Phase C: 測試
```
[003-T11] [P2] [US-1] 測試：input validation（缺 index.html、不合法檔案）
[003-T12] [P2] [US-1] 測試：完整 deploy 流程 integration test
[003-T13] [P2] [US-1] 測試：部分 deploy 失敗 case
```
