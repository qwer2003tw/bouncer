# Tasks: upload_scanner fail-open → fail-closed

Sprint: 60 | Task: bouncer-s60-001

## Phase 1: Setup

```bash
cd /home/ec2-user/projects/bouncer
git worktree add /tmp/s60-001-upload-scanner feat/sprint60-001-upload-scanner-fail-closed -b feat/sprint60-001-upload-scanner-fail-closed
cd /tmp/s60-001-upload-scanner
```

## Phase 2: Implementation

### Task 2.1：修改 `src/upload_scanner.py`（90 行，全檔改動）

**2.1a — 新增 import**
- Line 1-5 區塊：加入 `from metrics import emit_metric` 和 Powertools Logger
- `from aws_lambda_powertools import Logger`
- `logger = Logger(service="bouncer")`

**2.1b — 修改 inner decode exception（line 70-71）**
```python
# 原：
except Exception:
    return UploadScanResult(risk_level='safe', summary='')

# 改為 fail-closed + metric + log
except Exception as e:
    emit_metric('Bouncer', 'ScannerError', 1, dimensions={'Filename': filename[:50]})
    logger.error("Scanner decode error", extra={
        "src_module": "upload_scanner", "operation": "scan_upload",
        "filename": filename, "error_type": type(e).__name__, "error": str(e),
    })
    return UploadScanResult(risk_level='error', findings=['scanner_error'], summary=f'Scanner error: {type(e).__name__}')
```

**2.1c — 修改 outer exception（line 89-90）**
```python
# 原：
except Exception:  # noqa: BLE001 — fail-open: never block on scanner error
    return UploadScanResult(risk_level='safe', summary='')

# 改為 fail-closed + metric + log
except Exception as e:  # noqa: BLE001 — fail-closed: scanner error → human review
    emit_metric('Bouncer', 'ScannerError', 1, dimensions={'Filename': filename[:50]})
    logger.error("Scanner error during upload scan", extra={
        "src_module": "upload_scanner", "operation": "scan_upload",
        "filename": filename, "error_type": type(e).__name__, "error": str(e),
    })
    return UploadScanResult(risk_level='error', findings=['scanner_error'], summary=f'Scanner error: {type(e).__name__}')
```

**2.1d — 更新 docstring（line 40）**
```python
# 原：Returns UploadScanResult. Never raises — on exception returns safe (fail-open).
# 改為：Returns UploadScanResult. Never raises — on exception returns error (fail-closed, needs human review).
```

**2.1e — 更新 risk_level 註解（line 32）**
```python
# 原：risk_level: str = 'safe'  # 'blocked' / 'high' / 'medium' / 'safe'
# 改為：risk_level: str = 'safe'  # 'blocked' / 'high' / 'medium' / 'safe' / 'error'
```

### Task 2.2：修改 `src/mcp_upload.py`

**2.2a — 單檔上傳 risk_level 檢查（line 412）**
```python
# 原：if scan_result.risk_level in ('high', 'medium'):
# 改為：if scan_result.risk_level in ('high', 'medium', 'error'):
```

**2.2b — 批量上傳 risk_level 檢查（line 762）**
```python
# 原：if scan_result.risk_level in ('high', 'medium'):
# 改為：if scan_result.risk_level in ('high', 'medium', 'error'):
```

## Phase 3: Tests

**Test file：** `tests/test_upload_scanner.py`

### Task 3.1：新增 error path tests

```python
# Test 1: outer exception → fail-closed
def test_scan_upload_exception_returns_error():
    """Scanner crash → risk_level='error', not 'safe'"""
    # Mock regex module to raise → triggers outer except
    with patch('upload_scanner.re.search', side_effect=RuntimeError("regex engine boom")):
        result = scan_upload("test.py", b"AKIA1234567890123456", "text/plain")
        assert result.risk_level == 'error'
        assert 'scanner_error' in result.findings
        assert result.is_blocked is False

# Test 2: decode exception → fail-closed
def test_scan_upload_decode_exception_returns_error():
    """Decode failure → risk_level='error'"""
    # Force decode to raise by mocking
    with patch.object(bytes, 'decode', side_effect=MemoryError("out of memory")):
        result = scan_upload("test.txt", b"some bytes", "text/plain")
        assert result.risk_level == 'error'

# Test 3: metric emission on error
def test_scan_upload_error_emits_metric():
    with patch('upload_scanner.emit_metric') as mock_metric:
        with patch('upload_scanner.re.search', side_effect=Exception("boom")):
            scan_upload("test.py", b"content", "text/plain")
            mock_metric.assert_called_once()
            args = mock_metric.call_args
            assert args[0][1] == 'ScannerError'

# Test 4: normal scan unaffected
def test_scan_upload_normal_safe_unchanged():
    result = scan_upload("readme.txt", b"hello world", "text/plain")
    assert result.risk_level == 'safe'

# Test 5: blocked extension unaffected
def test_scan_upload_blocked_unchanged():
    result = scan_upload("virus.exe", b"MZ...", "application/octet-stream")
    assert result.risk_level == 'blocked'
    assert result.is_blocked is True
```

### Task 3.2：驗證 mcp_upload.py 的 error handling

```python
# 在 test_mcp_upload 或 test_upload_batch 中新增
# Test: scan error → file goes to human review (not silently approved)
def test_upload_scan_error_requires_human_review():
    with patch('mcp_upload.scan_upload') as mock_scan:
        mock_scan.return_value = UploadScanResult(
            risk_level='error',
            findings=['scanner_error'],
            summary='Scanner error: RuntimeError',
        )
        # assert upload goes to approval path (reason contains warning)
```

### Task 3.3：跑既有 tests 確認無 regression

```bash
cd /tmp/s60-001-upload-scanner
python -m pytest tests/test_upload_scanner.py -v
python -m pytest tests/ -k "upload" -v
```

## Phase 4: Lint & Commit

```bash
ruff check src/upload_scanner.py src/mcp_upload.py
git add src/upload_scanner.py src/mcp_upload.py tests/test_upload_scanner.py
git commit -m "feat(security): upload_scanner fail-open → fail-closed (#s60-001)

- Scanner exception now returns risk_level='error' instead of 'safe'
- Emit CloudWatch ScannerError metric on scanner failure
- Add structured error logging with Powertools Logger
- Update mcp_upload.py to handle 'error' risk level (human review)
- Inner decode exception also fail-closed
"
```

## TCS Summary

TCS=4 → 1 agent timeout 600s
