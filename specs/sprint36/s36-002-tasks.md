# S36-002: Plan + Tasks

## TCS: 12 (Medium)

## Plan

### Phase 1: upload_scanner.py（新檔案）
```python
# src/upload_scanner.py

@dataclass
class ScanResult:
    is_blocked: bool       # True = 自動拒絕
    risk_level: str        # 'blocked' / 'high' / 'medium' / 'safe'
    findings: list         # [(type, description)]
    summary: str           # 人讀摘要

def scan_file(filename: str, content_bytes: bytes, content_type: str) -> ScanResult:
    """Main entry point — scan filename + content"""
    # 1. extension check → blocked
    # 2. content type check
    # 3. secret pattern scan（只對文字類型）
    # return ScanResult
```

### Phase 2: mcp_upload.py 整合
在 `_submit_upload_for_approval()` 前加掃描：
```python
scan = scan_file(ctx.filename, ctx.content_bytes, ctx.content_type)
if scan.is_blocked:
    return mcp_error(req_id, f"Upload rejected: {scan.summary}")
if scan.risk_level in ('high', 'medium'):
    ctx.scan_findings = scan.findings  # 傳給 approval notification
```

### Phase 3: 通知整合
在 `send_upload_approval_notification()` 加 scan findings 顯示。

## Tasks

### Research
```
[T1] 確認 _submit_upload_for_approval 的 content bytes 取得方式
[T2] 確認 batch upload 的 content 傳遞方式
[T3] 確認現有 upload notification 函數位置
```

### Implementation
```
[T4] src/upload_scanner.py — BLOCKED_EXTENSIONS + SECRET_PATTERNS
[T5] scan_file() — extension check + text decode + regex scan
[T6] mcp_upload.py — 單一 upload 整合掃描
[T7] mcp_upload.py — batch upload 整合掃描
[T8] notifications.py — scan findings 顯示
```

### Tests
```
[T9]  test: .exe → is_blocked=True
[T10] test: .sh → is_blocked=True
[T11] test: .yaml with AWS key → risk_level=high, not blocked
[T12] test: normal .json → risk_level=safe
[T13] test: scan failure → fail-safe, not blocked
[T14] test: trust session upload still works
```

### Lint + CI
```
[T15] ruff check src/
[T16] git commit --no-verify
[T17] push → CI pass
```

## Success Metrics
- .exe/.sh 自動拒絕，不進 Telegram 審批 ✅
- .yaml 含 secret → 審批通知有高風險警告 ✅
- 正常檔案不受影響 ✅
