# Sprint 10-003: Tasks — bouncer_exec.sh pipe + --reason parsing

> Generated: 2026-03-03

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | bouncer_exec.sh (1 file) |
| D2 Cross-module | 0 | 純 client-side shell script，不影響 Lambda |
| D3 Testing | 0 | 手動驗證（shell script 無 unit test framework） |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 0 | 無新 AWS service |
| **Total TCS** | **1** | ✅ 不需拆分 |

## Task List

```
[003-T1] [P1] [US-2] 重寫 argument parsing — --reason/--account 支援任意位置
[003-T2] [P1] [US-1] COMMAND 構建改用 array + printf '%q' 保留 quoting
[003-T3] [P1] REASON fallback 改用 AWS_ARGS array
[003-T4] [P2] 更新 script header comment（usage + pipe 使用說明）
[003-T5] [P2] bouncer-exec SKILL.md 加 pipe escape 使用指南
[003-T6] [P2] 手動驗證：5 個 test cases
```
