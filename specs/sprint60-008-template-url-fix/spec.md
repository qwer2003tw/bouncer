# Feature Specification: template_s3_url 格式驗證

Feature Branch: feat/sprint60-008-template-url-fix
Sprint: 60
Task ID: bouncer-s60-008

## Problem Statement

`src/deployer.py` 的 auto_approve_code_only 流程（line ~1085）從 DynamoDB project config 讀取 `template_s3_url`，未驗證格式即傳遞給 `changeset_analyzer.create_dry_run_changeset()`。

當 `template_s3_url` 設定為無效值（例如空字串、非 HTTPS URL、不含 S3 域名）時：
1. `create_dry_run_changeset()` 呼叫 CloudFormation `create_change_set(TemplateURL=invalid)`
2. CloudFormation 回傳 `ValidationError`
3. 被 deployer 的 `except Exception` 捕獲，fallback 到人工審批
4. **持續產生 error log** — 每次 deploy 都重複發生

### 現行程式碼路徑

```python
# deployer.py line ~1085
template_s3_url = project.get('template_s3_url', '')

# line ~1094 — 只檢查空字串
elif not template_s3_url:
    logger.warning("auto_approve_code_only enabled but template_s3_url not set", ...)
    context = f"[auto_approve_code_only: template_s3_url 未設定] {context or ''}"
    # Fall through to human approval

# line ~1108 — 無格式驗證，直接傳給 changeset_analyzer
changeset_name = create_dry_run_changeset(
    _get_cfn_client(),
    stack_name,
    template_s3_url,  # ← 可能是任何字串
)
```

```python
# changeset_analyzer.py line ~128 — 直接傳給 CloudFormation
cfn_client.create_change_set(
    StackName=stack_name,
    TemplateURL=template_s3_url,  # ← 無驗證
    ...
)
```

---

## User Scenarios & Testing

### User Story 1：驗證 template_s3_url 格式

> 作為開發者，我需要 template_s3_url 在傳遞給 CloudFormation 之前經過格式驗證，以避免不必要的 API error。

**Given** project config 中 `template_s3_url` 設為 `"s3://bucket/template.yaml"`（非 HTTPS）
**When** auto_approve_code_only 嘗試建立 changeset
**Then** 在呼叫 CloudFormation 之前即檢測到無效格式
**And** 記錄 structured warning log 說明具體原因
**And** fallback 到人工審批（與現行 behavior 一致）

**Given** `template_s3_url` 為有效 HTTPS S3 URL（如 `https://bucket.s3.us-east-1.amazonaws.com/template.yaml`）
**When** auto_approve_code_only 嘗試建立 changeset
**Then** 正常流程不受影響

### User Story 2：Structured log 記錄具體原因

> 作為 DevOps 工程師，我需要 log 中明確記錄 template_s3_url 驗證失敗的原因，以便快速修復配置。

**Given** `template_s3_url` 格式無效
**When** 格式驗證失敗
**Then** log 包含：
  - 具體驗證失敗原因（如 "does not start with https://"、"does not contain S3 domain"）
  - 傳入的 URL 值（脫敏至前 100 字元）
  - `src_module="deployer"`, `operation="validate_template_url"`

---

## Requirements

### FR-001：建立 URL 驗證函數
- 在 `src/deployer.py` 或 `src/changeset_analyzer.py` 新增 `validate_template_s3_url(url: str) -> Tuple[bool, str]`
- 驗證規則：
  1. 必須以 `https://` 開頭
  2. 必須包含 S3 domain pattern（`.s3.` 或 `.s3-`）
  3. URL 長度不超過 1024（CloudFormation TemplateURL 限制）
- 回傳 `(True, "")` 或 `(False, "具體原因")`

### FR-002：在 deployer.py 中使用驗證
- 在 `template_s3_url` 非空後、呼叫 `create_dry_run_changeset` 之前，呼叫驗證函數
- 驗證失敗 → log warning + fallback to human approval
- **不改現有的 fallback 行為**（仍然到人工審批，只是更早發現錯誤）

### FR-003：Structured warning log
- Log level: WARNING（不是 ERROR，因為 fallback 是正常的）
- Extra fields：
  - `src_module="deployer"`
  - `operation="validate_template_url"`
  - `project_id=project_id`
  - `invalid_url=template_s3_url[:100]`
  - `validation_reason=reason`

### FR-004：不改 changeset_analyzer.py
- `create_dry_run_changeset()` 不加驗證（它是通用 function，呼叫端負責驗證）
- 職責分離：deployer 驗證 → changeset_analyzer 執行

---

## Interface Contract

### 新增 validate function（放在 deployer.py）

```python
def validate_template_s3_url(url: str) -> tuple[bool, str]:
    """Validate template_s3_url format before passing to CloudFormation.

    Rules:
    - Must start with https://
    - Must contain S3 domain (.s3. or .s3-)
    - Max length 1024

    Returns:
        (is_valid, reason) — reason is empty string when valid.
    """
    if not url:
        return False, "URL is empty"
    if len(url) > 1024:
        return False, f"URL too long ({len(url)} > 1024)"
    if not url.startswith('https://'):
        return False, f"URL does not start with https:// (got: {url[:30]}...)"
    if '.s3.' not in url and '.s3-' not in url:
        return False, f"URL does not contain S3 domain (.s3. or .s3-)"
    return True, ""
```

### deployer.py 使用位置

```python
# 在 elif not template_s3_url: 之後（line ~1099），else: 之前，新增：
else:
    # Validate URL format before calling CloudFormation
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
        # Fall through to human approval
    else:
        # existing changeset logic...
```

---

## TCS 計算

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| D1 Files | 1/5 | 1 file：`deployer.py` |
| D2 Cross-module | 0/4 | 無跨模組影響 |
| D3 Testing | 1/4 | 驗證函數的 unit tests（幾個 cases）|
| D4 Infrastructure | 0/4 | 無 template.yaml 改動 |
| D5 External | 0/4 | 減少 CloudFormation API error calls（正面影響）|

**Total TCS: 2 (Simple)**
→ Sub-agent strategy: 1 agent timeout 600s

---

## Cost Analysis

- **減少成本**：減少無效的 CloudFormation `CreateChangeSet` API calls
- CloudFormation API calls 本身免費，但減少 Lambda 執行時間（少一次 API call + retry）
- **新增成本：$0**

---

## Success Criteria

- SC-001：無效 URL（非 HTTPS、缺 S3 domain）在 CloudFormation 呼叫前被攔截
- SC-002：有效 URL 正常通過（不影響 auto_approve_code_only 流程）
- SC-003：Structured log 包含具體驗證失敗原因
- SC-004：Fallback 行為不變（仍然到人工審批）
- SC-005：所有既有 deploy tests 通過
- SC-006：新增 validate_template_s3_url 至少 5 個 test cases（valid、empty、non-https、no-s3、too-long）
