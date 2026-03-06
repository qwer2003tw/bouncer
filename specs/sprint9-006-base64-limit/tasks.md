# Sprint 9-006: Tasks — base64 CLI args 超長

> Generated: 2026-03-02

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | 1 script file (bouncer_upload_http.sh 或整合到 bouncer-exec) |
| D2 Cross-module | 0 | Client-side 改動，不影響 Bouncer Lambda |
| D3 Testing | 0 | Script 層級，不需 pytest |
| D4 Infrastructure | 0 | 無 |
| D5 External | 0 | 使用現有 HTTP API |
| **Total TCS** | **1** | ✅ 不需拆分 |

## Task List

```
[006-T1] [P2] [US-1] 新增 bouncer_upload_http.sh：接受 JSON 檔案路徑 → POST 到 Bouncer MCP endpoint
[006-T2] [P2] [US-1] bouncer-exec skill 整合：判斷 payload 大小自動切換 CLI/HTTP
[006-T3] [P2] [US-1] TOOLS.md 更新：HTTP direct call 改為推薦方式（移除 workaround 標記）
[006-T4] [P3] [US-1] 提 mcporter upstream feature request（--args-file / --args-stdin）
```
