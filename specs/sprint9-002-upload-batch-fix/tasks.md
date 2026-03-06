# Sprint 9-002: Tasks — upload_batch 大檔案靜默失敗根因修復

> Generated: 2026-03-02

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 3 | constants.py, mcp_upload.py, tool_schema.py (3 files) |
| D2 Cross-module | 0 | 修改集中在 mcp_upload.py，constants 只是 import |
| D3 Testing | 2 | 補測試（新 validation + 部分失敗） |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 0 | 無新 AWS service |
| **Total TCS** | **5** | ✅ 不需拆分 |

## Task List

```
[002-T1] [P1] [US-2] constants.py: 新增 UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT 和 UPLOAD_SINGLE_PAYLOAD_SAFE_LIMIT
[002-T2] [P1] [US-1] mcp_upload.py: mcp_tool_upload_batch() 加入 base64 payload 總長度 early check
[002-T3] [P1] [US-2] mcp_upload.py: 超過 limit 時返回 error + suggestion 用 presigned
[002-T4] [P2] [US-2] mcp_upload.py: mcp_tool_upload() 加入 base64 string 長度 early check
[002-T5] [P2] [US-1] mcp_upload.py: trust 路徑 batch upload 部分失敗處理（成功/失敗分離）
[002-T6] [P2] [US-2] tool_schema.py: 更新 bouncer_upload_batch description 加入大小限制說明
[002-T7] [P2] [US-1] 測試：payload 超過 safe limit 的 error response
[002-T8] [P2] [US-1] 測試：trust 路徑部分失敗的 response 結構
```
