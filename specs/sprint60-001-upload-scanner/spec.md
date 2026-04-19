# Feature Specification: upload_scanner fail-open → fail-closed

Feature Branch: feat/sprint60-001-upload-scanner-fail-closed
Sprint: 60
Task ID: bouncer-s60-001

## Problem Statement

`src/upload_scanner.py` line 89 的 `except Exception` catch-all 回傳 `UploadScanResult(risk_level='safe')`，導致 scanner crash 時檔案被視為安全（fail-open）。這是嚴重的安全漏洞：任何導致 scanner exception 的檔案都能繞過安全掃描。

### 現行行為（有問題）

```python
except Exception:  # noqa: BLE001 — fail-open: never block on scanner error
    return UploadScanResult(risk_level='safe', summary='')
```

### 受影響的呼叫點

1. `src/mcp_upload.py:389` — 單檔上傳 `scan_upload(ctx.filename, content_bytes, ctx.content_type)`
2. `src/mcp_upload.py:730` — 批量上傳 `scan_upload(safe_name, content_bytes, ct)`

兩處都只處理 `is_blocked=True` 和 `risk_level in ('high', 'medium')`，**沒有處理 `risk_level='error'`** 的路徑。

---

## User Scenarios & Testing

### User Story 1：Scanner error → fail-closed

> 作為 Bouncer 系統管理員，我需要 scanner 在 crash 時拒絕放行檔案（fail-closed），以確保安全掃描無法被繞過。

**Given** 一個上傳的檔案觸發了 scanner 內部 exception（如 regex engine error、記憶體不足）
**When** `scan_upload()` 的 outer `except Exception` 被觸發
**Then** 回傳 `UploadScanResult(risk_level='error', is_blocked=False, summary='Scanner error: ...')`
**And** 該上傳需經人工審批（與 `risk_level='high'` 相同路徑）

**Given** scanner 正常運作
**When** 掃描一個普通檔案（無秘鑰、非封鎖副檔名）
**Then** 行為與現行完全一致，回傳 `risk_level='safe'`

### User Story 2：Scanner error → CloudWatch metric + structured log

> 作為 DevOps 工程師，我需要 scanner error 被記錄為 CloudWatch metric 和 structured log，以便監控和告警。

**Given** scanner 發生 exception
**When** 進入 error handling 路徑
**Then** 發射 CloudWatch EMF metric `ScannerError`（Namespace: `Bouncer`）
**And** 使用 Powertools Logger 記錄 structured log，包含 `filename`、`error_type`、`error_message`
**And** log level 為 `ERROR`

---

## Requirements

### FR-001：fail-closed error handling
- `scan_upload()` 的 outer `except Exception`（line 89）改為回傳 `risk_level='error'`
- `is_blocked` 保持 `False`（不直接拒絕，但需 human review）
- `summary` 包含 exception 類型和訊息（脫敏，不含 stack trace）
- `findings` 包含 `['scanner_error']` 標記

### FR-002：error risk level 的下游處理
- `mcp_upload.py` 的 `scan_result.risk_level` 檢查需加入 `'error'`
- `risk_level='error'` 的檔案與 `'high'` 相同處理路徑：加入 reason warning，需人工審批
- 單檔上傳（line 412）和批量上傳（line 762）都要更新

### FR-003：CloudWatch metric emission
- 在 error path 呼叫 `emit_metric('Bouncer', 'ScannerError', 1, dimensions={'Filename': filename[:50]})`
- 需 import `metrics.emit_metric`

### FR-004：Structured error logging
- 使用 Powertools Logger 記錄 error 事件
- Extra fields：`src_module="upload_scanner"`, `operation="scan_upload"`, `filename=filename`, `error_type=type(e).__name__`, `error=str(e)`

### FR-005：inner decode exception 也改為 fail-closed
- `content_bytes.decode()` 的 `except Exception`（line 70-71）同樣改為 `risk_level='error'`
- 使用同樣的 metric 和 log 格式

---

## Interface Contract

### UploadScanResult 變更

```python
@dataclass
class UploadScanResult:
    is_blocked: bool = False
    risk_level: str = 'safe'  # 'blocked' / 'high' / 'medium' / 'safe' / 'error' ← 新增
    findings: List[str] = field(default_factory=list)
    summary: str = ''
```

### mcp_upload.py 的 risk_level 處理

```python
# 現行（line 412, 762）：
if scan_result.risk_level in ('high', 'medium'):

# 改為：
if scan_result.risk_level in ('high', 'medium', 'error'):
```

### scan_upload() error path 新簽名

```python
except Exception as e:  # noqa: BLE001 — fail-closed: scanner error → human review
    emit_metric('Bouncer', 'ScannerError', 1, dimensions={'Filename': filename[:50]})
    logger.error("Scanner error during upload scan", extra={
        "src_module": "upload_scanner",
        "operation": "scan_upload",
        "filename": filename,
        "error_type": type(e).__name__,
        "error": str(e),
    })
    return UploadScanResult(
        risk_level='error',
        findings=['scanner_error'],
        summary=f'Scanner error: {type(e).__name__}',
    )
```

---

## TCS 計算

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| D1 Files | 1/5 | 2 files：`upload_scanner.py`、`mcp_upload.py` |
| D2 Cross-module | 1/4 | `mcp_upload.py` 只需更新 risk_level 檢查條件 |
| D3 Testing | 2/4 | 需測試 error path（outer + inner exception）、正常行為不受影響、metric emission |
| D4 Infrastructure | 0/4 | 無 template.yaml / infra 改動 |
| D5 External | 0/4 | 無外部 API / 第三方整合 |

**Total TCS: 4 (Simple)**
→ Sub-agent strategy: 1 agent timeout 600s

---

## Cost Analysis

- **CloudWatch EMF metric `ScannerError`**：EMF 透過 Lambda stdout 免費發射，CloudWatch custom metric 前 10 個免費，之後 $0.30/metric/month。此 metric 僅在 error 時發射，預期極低量。
- **無額外 infra 成本**

---

## Success Criteria

- SC-001：scanner exception 時回傳 `risk_level='error'`，不回傳 `'safe'`
- SC-002：正常掃描行為（blocked、high、safe）完全不變
- SC-003：CloudWatch metric `ScannerError` 正確發射
- SC-004：structured log 包含完整 error 資訊
- SC-005：`mcp_upload.py` 的單檔和批量上傳都正確處理 `error` risk level
- SC-006：所有既有 upload_scanner tests 通過
- SC-007：新增 error path 測試覆蓋率 ≥ 4 個 test cases
