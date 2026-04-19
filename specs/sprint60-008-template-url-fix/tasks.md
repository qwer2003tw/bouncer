# Tasks: template_s3_url 格式驗證

Sprint: 60 | Task: bouncer-s60-008

## Phase 1: Setup

```bash
cd /home/ec2-user/projects/bouncer
git worktree add /tmp/s60-008-template-url-fix feat/sprint60-008-template-url-fix -b feat/sprint60-008-template-url-fix
cd /tmp/s60-008-template-url-fix
```

## Phase 2: Analysis

### Task 2.1：確認完整的 auto_approve_code_only 流程

```bash
sed -n '1080,1130p' src/deployer.py
```

### Task 2.2：確認 changeset_analyzer 的 TemplateURL 使用

```bash
sed -n '98,135p' src/changeset_analyzer.py
```

### Task 2.3：確認既有 test 覆蓋

```bash
grep -n "template_s3_url\|auto_approve_code_only" tests/test_deployer_main.py tests/test_changeset_analyzer.py tests/test_sprint32_deploy_auto_approve.py 2>/dev/null | head -20
```

## Phase 3: Implementation

### Task 3.1：新增 `validate_template_s3_url` function

在 `src/deployer.py` 適當位置（建議在 helper functions 區塊），新增：

```python
def validate_template_s3_url(url: str) -> tuple:
    """Validate template_s3_url format before passing to CloudFormation.

    Args:
        url: The S3 URL to validate.

    Returns:
        (is_valid: bool, reason: str) — reason is empty when valid.
    """
    if not url:
        return False, "URL is empty"
    if len(url) > 1024:
        return False, f"URL too long ({len(url)} > 1024)"
    if not url.startswith('https://'):
        return False, f"URL does not start with https://"
    if '.s3.' not in url and '.s3-' not in url:
        return False, "URL does not contain S3 domain (.s3. or .s3-)"
    return True, ""
```

### Task 3.2：在 auto_approve_code_only 流程中加入驗證

修改 `src/deployer.py` 的 auto_approve_code_only 區塊（line ~1100 的 else 分支）。

在 `changeset_name = create_dry_run_changeset(...)` 呼叫之前，加入：

```python
        else:
            # Validate URL format before calling CloudFormation (s60-008)
            is_valid, validation_reason = validate_template_s3_url(template_s3_url)
            if not is_valid:
                logger.warning("auto_approve_code_only: invalid template_s3_url", extra={
                    "src_module": "deployer",
                    "operation": "validate_template_url",
                    "project_id": project_id,
                    "invalid_url": template_s3_url[:100],
                    "validation_reason": validation_reason,
                })
                context = f"[auto_approve_code_only: template_s3_url 格式無效 — {validation_reason}] {context or ''}"
                # Fall through to human approval (same as empty URL case)
            else:
                changeset_name = None
                try:
                    changeset_name = create_dry_run_changeset(
                        _get_cfn_client(),
                        stack_name,
                        template_s3_url,
                    )
                    # ... rest of existing changeset logic ...
```

⚠️ **注意縮排**：需確認 if/else 的 nesting level 與現有 code 一致。可能需要把整個 existing changeset block 包在 `else:` 中。

## Phase 4: Tests

### Task 4.1：validate_template_s3_url unit tests

在 `tests/test_deployer_main.py` 或新建 `tests/test_sprint60_template_url.py`：

```python
from deployer import validate_template_s3_url

def test_valid_s3_url():
    ok, reason = validate_template_s3_url("https://my-bucket.s3.us-east-1.amazonaws.com/template.yaml")
    assert ok is True
    assert reason == ""

def test_valid_s3_url_path_style():
    ok, reason = validate_template_s3_url("https://s3.us-east-1.amazonaws.com/my-bucket/template.yaml")
    assert ok is True

def test_empty_url():
    ok, reason = validate_template_s3_url("")
    assert ok is False
    assert "empty" in reason.lower()

def test_non_https_url():
    ok, reason = validate_template_s3_url("http://bucket.s3.amazonaws.com/template.yaml")
    assert ok is False
    assert "https" in reason.lower()

def test_s3_protocol_url():
    ok, reason = validate_template_s3_url("s3://bucket/template.yaml")
    assert ok is False
    assert "https" in reason.lower()

def test_no_s3_domain():
    ok, reason = validate_template_s3_url("https://example.com/template.yaml")
    assert ok is False
    assert "S3 domain" in reason or "s3" in reason.lower()

def test_too_long_url():
    ok, reason = validate_template_s3_url("https://bucket.s3.amazonaws.com/" + "a" * 1024)
    assert ok is False
    assert "long" in reason.lower()

def test_valid_s3_dash_url():
    """s3-region format is also valid"""
    ok, reason = validate_template_s3_url("https://s3-us-east-1.amazonaws.com/bucket/template.yaml")
    assert ok is True
```

### Task 4.2：Integration test — 無效 URL fallback

```python
def test_invalid_template_url_falls_through_to_manual():
    """invalid template_s3_url → log warning + human approval"""
    # Mock project with invalid URL
    project = {'template_s3_url': 's3://invalid', 'stack_name': 'test-stack', 'auto_approve_code_only': True}
    # ... invoke deploy flow
    # Assert: no CloudFormation call made
    # Assert: logger.warning called with validate_template_url
```

### Task 4.3：跑既有 deploy tests

```bash
python -m pytest tests/test_deployer_main.py tests/test_changeset_analyzer.py tests/test_sprint32_deploy_auto_approve.py -v
python -m pytest tests/ -x --tb=short 2>&1 | tail -30
```

## Phase 5: Lint & Commit

```bash
ruff check src/deployer.py
git add src/deployer.py tests/
git commit -m "fix: validate template_s3_url format before CloudFormation call (#s60-008)

- Add validate_template_s3_url(): checks https://, S3 domain, max length
- Invalid URL → structured warning log + fallback to human approval
- Eliminates unnecessary CloudFormation API errors for misconfigured projects
- No behavior change for valid URLs
"
```

## TCS Summary

TCS=2 → 1 agent timeout 600s（最簡單的 task）
