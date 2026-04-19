# Spec: Code-Only Deploy 通知加 Git Diff 摘要

**Feature ID:** s39-001
**Branch:** `feat/code-only-diff-summary-s39`
**Epic:** Deploy Workflow Enhancements
**Story Points:** 3

---

## Overview

當 deploy 因「純 code 變更」被 auto-approve 時，通知訊息應包含變更摘要，讓審核者快速了解哪些資源被修改。

**Context:**
- 目前 `send_auto_approve_deploy_notification` 只顯示專案、Deploy ID、來源、原因
- 呼叫點有兩個：
  1. `auto_approve` (deployer.py:987) - 可用 `diff_result.diff_summary`
  2. `auto_approve_code_only` (deployer.py:1060) - 可用 `changeset_result.resource_changes`
- 審核者需要看到「什麼被改了」才能判斷 auto-approve 是否合理

---

## User Stories

### Story 1: Template Diff Summary in Notification
**As a** deploy reviewer
**I want** to see the git diff summary in the auto-approve notification
**So that** I can quickly verify what infrastructure changes were detected

**Acceptance Criteria:**
- [ ] `send_auto_approve_deploy_notification` 接受新的 optional parameter `changes_summary: str = ''`
- [ ] 當 `changes_summary` 非空時，通知底部顯示：`📋 *變更：* {changes_summary}`
- [ ] 當 `changes_summary` 為空時，通知底部顯示：`_(無變更明細)_`
- [ ] 通知格式保持與現有 auto-approve 通知一致

### Story 2: Auto-Approve Path Provides Diff Summary
**As a** developer
**When** my deploy is auto-approved via `auto_approve` function
**Then** the notification should include the template diff summary

**Acceptance Criteria:**
- [ ] `auto_approve` (deployer.py:987) 呼叫 `send_auto_approve_deploy_notification` 時傳入 `diff_result.diff_summary`
- [ ] diff_summary 格式：`"Lambda: ApprovalFunction, S3: Bucket"`（已由 template_diff_analyzer 提供）
- [ ] 若 diff_result.diff_summary 為 None 或空字串，傳空字串

### Story 3: Code-Only Path Provides Changeset Summary
**As a** developer
**When** my deploy is auto-approved via `auto_approve_code_only` function
**Then** the notification should include the CloudFormation changeset summary

**Acceptance Criteria:**
- [ ] `auto_approve_code_only` (deployer.py:1060) 呼叫 `send_auto_approve_deploy_notification` 時傳入格式化的 `changeset_result.resource_changes`
- [ ] resource_changes 格式化為：`"Lambda: LogicalId (Action), LogicalId2 (Action)"`
- [ ] 範例：`"Lambda: ApprovalFunction (Modify), DeployFunction (Modify)"`
- [ ] 若 resource_changes 為空，傳空字串
- [ ] 錯誤處理：若 changeset_result.error 存在，變更摘要應為 `"changeset 分析失敗"`

---

## Interface Contract

### Function Signature Change

**Before:**
```python
def send_auto_approve_deploy_notification(
    project_id: str,
    deploy_id: str,
    source: Optional[str] = None,
    reason: str = '',
) -> None:
```

**After:**
```python
def send_auto_approve_deploy_notification(
    project_id: str,
    deploy_id: str,
    source: Optional[str] = None,
    reason: str = '',
    changes_summary: str = '',  # NEW
) -> None:
```

### Notification Format

**Current format:**
```
🚀 自動批准 Deploy

📦 專案：bouncer-api
🆔 Deploy ID：d-abc123
📍 來源：local
💡 原因：純 code 變更，CFN changeset 分析通過
```

**New format:**
```
🚀 自動批准 Deploy

📦 專案：bouncer-api
🆔 Deploy ID：d-abc123
📍 來源：local
💡 原因：純 code 變更，CFN changeset 分析通過
📋 *變更：* Lambda: ApprovalFunction (Modify), DeployFunction (Modify)
```

---

## Implementation Details

### 1. Update `send_auto_approve_deploy_notification` (src/notifications.py:934)

**Location:** `src/notifications.py:934`

```python
def send_auto_approve_deploy_notification(
    project_id: str,
    deploy_id: str,
    source: Optional[str] = None,
    reason: str = '',
    changes_summary: str = '',  # NEW parameter
) -> None:
    """
    Send notification when deploy is auto-approved.

    Args:
        project_id: Project identifier
        deploy_id: Deploy identifier
        source: Deploy source (e.g., 'local', 'github')
        reason: Reason for auto-approval
        changes_summary: Summary of changes (git diff or changeset)
    """
    mb = MessageBuilder()
    mb.text("🚀 ").bold("自動批准 Deploy").newline().newline()
    mb.text("📦 ").bold("專案：").text(project_id).newline()
    mb.text("🆔 ").bold("Deploy ID：").text(deploy_id).newline()

    if source:
        mb.text("📍 ").bold("來源：").text(source).newline()

    if reason:
        mb.text("💡 ").bold("原因：").text(reason).newline()

    # NEW: Add changes summary
    if changes_summary:
        mb.text("📋 ").bold("變更：").text(changes_summary).newline()
    else:
        mb.italic("(無變更明細)").newline()

    send_message(
        chat_id=get_notification_chat_id(project_id),
        message_builder=mb,
    )
```

### 2. Update `auto_approve` Call (src/deployer.py:987)

**Location:** `src/deployer.py:987-988`

**Before:**
```python
send_auto_approve_deploy_notification(
    project_id=project_id,
    deploy_id=deploy_result.get('deploy_id', ''),
    source=source,
    reason=reason,
)
```

**After:**
```python
# Extract diff summary from diff_result
changes_summary = diff_result.diff_summary if diff_result and diff_result.diff_summary else ''

send_auto_approve_deploy_notification(
    project_id=project_id,
    deploy_id=deploy_result.get('deploy_id', ''),
    source=source,
    reason=reason,
    changes_summary=changes_summary,
)
```

### 3. Update `auto_approve_code_only` Call (src/deployer.py:1060)

**Location:** `src/deployer.py:1060-1061`

**Before:**
```python
send_auto_approve_deploy_notification(
    project_id=project_id,
    deploy_id=deploy_result.get('deploy_id', ''),
    source=source,
    reason=reason,
)
```

**After:**
```python
# Format resource_changes into summary
changes_summary = format_changeset_summary(changeset_result)

send_auto_approve_deploy_notification(
    project_id=project_id,
    deploy_id=deploy_result.get('deploy_id', ''),
    source=source,
    reason=reason,
    changes_summary=changes_summary,
)
```

### 4. Add Helper Function `format_changeset_summary` (src/deployer.py)

**Location:** New function in `src/deployer.py` (near top of file)

```python
def format_changeset_summary(changeset_result) -> str:
    """
    Format CloudFormation changeset resource_changes into a readable summary.

    Args:
        changeset_result: Changeset result with resource_changes list

    Returns:
        Formatted string like "Lambda: Func1 (Modify), Func2 (Modify)"

    Example:
        Input: [
            {'ResourceChange': {
                'ResourceType': 'AWS::Lambda::Function',
                'Action': 'Modify',
                'LogicalResourceId': 'ApprovalFunction'
            }},
            {'ResourceChange': {
                'ResourceType': 'AWS::Lambda::Function',
                'Action': 'Modify',
                'LogicalResourceId': 'DeployFunction'
            }}
        ]
        Output: "Lambda: ApprovalFunction (Modify), DeployFunction (Modify)"
    """
    if not changeset_result or changeset_result.error:
        return "changeset 分析失敗"

    if not changeset_result.resource_changes:
        return ""

    # Group by resource type
    by_type = {}
    for change in changeset_result.resource_changes:
        rc = change.get('ResourceChange', {})
        resource_type = rc.get('ResourceType', '')
        action = rc.get('Action', '')
        logical_id = rc.get('LogicalResourceId', '')

        # Extract short type name (e.g., "AWS::Lambda::Function" -> "Lambda")
        short_type = resource_type.split('::')[1] if '::' in resource_type else resource_type

        if short_type not in by_type:
            by_type[short_type] = []
        by_type[short_type].append(f"{logical_id} ({action})")

    # Format output
    parts = []
    for resource_type, changes in sorted(by_type.items()):
        changes_str = ', '.join(changes)
        parts.append(f"{resource_type}: {changes_str}")

    return '; '.join(parts)
```

---

## Test Strategy

### Unit Tests

**File:** `tests/test_notifications.py`

```python
def test_send_auto_approve_notification_with_changes_summary():
    """Test auto-approve notification includes changes summary"""
    # Test with changes
    send_auto_approve_deploy_notification(
        project_id='test-project',
        deploy_id='d-123',
        source='local',
        reason='code only',
        changes_summary='Lambda: Func1 (Modify)',
    )
    # Assert message contains "📋 *變更：* Lambda: Func1 (Modify)"

def test_send_auto_approve_notification_without_changes_summary():
    """Test auto-approve notification shows no changes message"""
    send_auto_approve_deploy_notification(
        project_id='test-project',
        deploy_id='d-123',
        reason='code only',
        changes_summary='',
    )
    # Assert message contains "(無變更明細)"
```

**File:** `tests/test_deployer.py`

```python
def test_format_changeset_summary_multiple_resources():
    """Test changeset summary formatting with multiple resource types"""
    changeset_result = MockChangesetResult(
        resource_changes=[
            {'ResourceChange': {
                'ResourceType': 'AWS::Lambda::Function',
                'Action': 'Modify',
                'LogicalResourceId': 'ApprovalFunction'
            }},
            {'ResourceChange': {
                'ResourceType': 'AWS::Lambda::Function',
                'Action': 'Modify',
                'LogicalResourceId': 'DeployFunction'
            }},
            {'ResourceChange': {
                'ResourceType': 'AWS::S3::Bucket',
                'Action': 'Add',
                'LogicalResourceId': 'NewBucket'
            }}
        ]
    )

    result = format_changeset_summary(changeset_result)
    assert 'Lambda: ApprovalFunction (Modify), DeployFunction (Modify)' in result
    assert 'S3: NewBucket (Add)' in result

def test_format_changeset_summary_error():
    """Test changeset summary when analysis failed"""
    changeset_result = MockChangesetResult(error='API error')
    result = format_changeset_summary(changeset_result)
    assert result == "changeset 分析失敗"

def test_format_changeset_summary_empty():
    """Test changeset summary with no changes"""
    changeset_result = MockChangesetResult(resource_changes=[])
    result = format_changeset_summary(changeset_result)
    assert result == ""
```

### Integration Tests

**File:** `tests/integration/test_deploy_flow.py`

```python
def test_auto_approve_includes_diff_summary(mock_telegram):
    """Test auto-approve flow includes git diff summary in notification"""
    # Setup: deploy with code-only changes
    # Execute: trigger auto_approve
    # Assert: notification message contains diff_summary from diff_result

def test_auto_approve_code_only_includes_changeset_summary(mock_telegram):
    """Test code-only flow includes changeset summary in notification"""
    # Setup: deploy with CFN changeset
    # Execute: trigger auto_approve_code_only
    # Assert: notification message contains formatted resource_changes
```

### Manual Testing Checklist

- [ ] Deploy with code-only changes via `auto_approve` path → verify notification shows git diff summary
- [ ] Deploy with code-only changes via `auto_approve_code_only` path → verify notification shows changeset summary
- [ ] Deploy where diff_summary is None → verify notification shows "(無變更明細)"
- [ ] Deploy where changeset analysis fails → verify notification shows "changeset 分析失敗"
- [ ] Verify notification formatting is clean and readable
- [ ] Verify existing auto-approve behavior not broken

---

## Risk Assessment

**Low Risk:**
- Adds optional parameter with default value (backward compatible)
- Only affects notification display (no business logic changes)
- Changes isolated to notification and deployer modules

**Potential Issues:**
- Long changeset summaries might clutter notification → mitigation: truncate if > 200 chars
- Diff summary format inconsistent between two code paths → mitigation: document format expectations

---

## Rollout Plan

1. Implement `format_changeset_summary` helper + unit tests
2. Update `send_auto_approve_deploy_notification` signature + tests
3. Update `auto_approve` call site + integration test
4. Update `auto_approve_code_only` call site + integration test
5. Deploy to staging, trigger test deploys via both code paths
6. Monitor notifications for formatting issues
7. Deploy to production

---

## Related Issues

- Issue #43: Deploy progress visualization (same sprint)
- Previous: Template diff analyzer (provides diff_summary)
