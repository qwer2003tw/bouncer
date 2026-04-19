# Spec: Deploy Progress Checklist 增強

**Feature ID:** s39-002
**Branch:** `feat/deploy-checklist-s39`
**Epic:** Deploy UX Improvements
**Story Points:** 5

---

## Overview

改善 deploy progress checklist，加入 ANALYZING phase 並顯示每個 phase 的 elapsed time，讓使用者更清楚了解 deploy 進度和時間花費。

**Context:**
- 目前 `handle_progress` (deployer/notifier/app.py) 實作了 4-phase checklist
- Phases: INITIALIZING → SCANNING → BUILDING → DEPLOYING
- 使用 emoji 表示狀態：✅ (完成)、🔄 (進行中)、⏳ (等待中)
- Issue #43 需求：更好的 deploy progress visualization
- 缺少 changeset 分析階段（ANALYZING）的視覺化
- 沒有顯示各 phase 花費的時間

**Current behavior:**
```
Deploy 進度：
✅ 初始化
🔄 Template 掃描
⏳ sam build
⏳ sam deploy
```

**Desired behavior:**
```
Deploy 進度：
✅ 初始化 (2s)
✅ Changeset 分析 (5s)
🔄 Template 掃描 (已 3s)
⏳ sam build
⏳ sam deploy
```

---

## User Stories

### Story 1: Add ANALYZING Phase
**As a** developer
**I want** to see changeset analysis progress in the checklist
**So that** I know the deploy hasn't stalled during infrastructure review

**Acceptance Criteria:**
- [ ] 新增 `ANALYZING` phase 在 INITIALIZING 和 SCANNING 之間
- [ ] Phase emoji mapping: `'ANALYZING': ('✅', '🔄', '⏳', '⏳', '⏳')`
- [ ] Phase 順序：初始化 → Changeset 分析 → Template 掃描 → sam build → sam deploy
- [ ] 現有 4-phase 邏輯不受影響（backward compatible）

### Story 2: Display Elapsed Time per Phase
**As a** developer
**I want** to see how long each phase takes
**So that** I can identify slow steps in the deploy process

**Acceptance Criteria:**
- [ ] 已完成的 phase 顯示總耗時：`✅ 初始化 (2s)`
- [ ] 進行中的 phase 顯示已花費時間：`🔄 sam build (已 42s)`
- [ ] 等待中的 phase 不顯示時間
- [ ] 時間格式：< 60s 顯示 "Xs"，≥ 60s 顯示 "Xm Ys"
- [ ] Phase 開始時間從 SFN execution history 取得

### Story 3: Notifier Triggers ANALYZING Phase
**As a** system
**When** AnalyzeChangeset state starts in Step Functions
**Then** the notifier should update progress to ANALYZING

**Acceptance Criteria:**
- [ ] `handle_analyze` (notifier app.py) 呼叫 progress update API
- [ ] Progress update payload: `{"phase": "ANALYZING", "started_at": <timestamp>}`
- [ ] 若 AnalyzeChangeset 失敗，progress 保持在 ANALYZING 直到 HandleFailure
- [ ] 若 changeset 無 infra changes，phase 從 ANALYZING → SCANNING

---

## Technical Design

### 1. Update Phase Definitions (deployer/notifier/app.py)

**Location:** `deployer/notifier/app.py:handle_progress`

**Current implementation:**
```python
phases = {
    'INITIALIZING': ('🔄', '⏳', '⏳', '⏳'),  # (init, scan, build, deploy)
    'SCANNING': ('✅', '🔄', '⏳', '⏳'),
    'BUILDING': ('✅', '✅', '🔄', '⏳'),
    'DEPLOYING': ('✅', '✅', '✅', '🔄'),
}

phase_labels = ['初始化', 'Template 掃描', 'sam build', 'sam deploy']
```

**New implementation:**
```python
phases = {
    'INITIALIZING': ('🔄', '⏳', '⏳', '⏳', '⏳'),  # 5 phases now
    'ANALYZING': ('✅', '🔄', '⏳', '⏳', '⏳'),
    'SCANNING': ('✅', '✅', '🔄', '⏳', '⏳'),
    'BUILDING': ('✅', '✅', '✅', '🔄', '⏳'),
    'DEPLOYING': ('✅', '✅', '✅', '✅', '🔄'),
}

phase_labels = [
    '初始化',
    'Changeset 分析',
    'Template 掃描',
    'sam build',
    'sam deploy'
]
```

### 2. Add Elapsed Time Calculation

**New helper function in `deployer/notifier/app.py`:**

```python
from datetime import datetime, timezone

def format_elapsed_time(seconds: int) -> str:
    """
    Format elapsed time in human-readable format.

    Args:
        seconds: Elapsed seconds

    Returns:
        Formatted string like "2s" or "1m 30s"

    Examples:
        15 -> "15s"
        90 -> "1m 30s"
        3661 -> "61m 1s"
    """
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    remaining_seconds = seconds % 60
    if remaining_seconds == 0:
        return f"{minutes}m"
    return f"{minutes}m {remaining_seconds}s"


def get_phase_times(execution_arn: str, current_phase: str) -> dict[str, int]:
    """
    Extract phase start times from Step Functions execution history.

    Args:
        execution_arn: SFN execution ARN
        current_phase: Current phase name

    Returns:
        Dict mapping phase name to elapsed seconds
        Example: {'INITIALIZING': 2, 'ANALYZING': 5, 'SCANNING': 0}

    Implementation:
        - Call SFN DescribeExecution to get startDate
        - Call SFN GetExecutionHistory to get state transitions
        - Map state names to phases:
            - ExecutionStarted -> INITIALIZING
            - AnalyzeChangeset (StateEntered) -> ANALYZING
            - (any scanning state) -> SCANNING
            - (CodeBuild state) -> BUILDING
            - (after CodeBuild success) -> DEPLOYING
        - Calculate elapsed from phase start to now (if current) or to next phase
    """
    sfn_client = boto3.client('stepfunctions')

    # Get execution start time
    exec_details = sfn_client.describe_execution(executionArn=execution_arn)
    exec_start = exec_details['startDate']

    # Get execution history
    history = sfn_client.get_execution_history(
        executionArn=execution_arn,
        maxResults=100,
        reverseOrder=False
    )

    phase_times = {}
    phase_starts = {}
    now = datetime.now(timezone.utc)

    # Parse history to find phase transitions
    for event in history['events']:
        event_type = event['type']
        timestamp = event['timestamp']

        if event_type == 'ExecutionStarted':
            phase_starts['INITIALIZING'] = timestamp

        elif event_type == 'TaskStateEntered':
            state_name = event.get('stateEnteredEventDetails', {}).get('name', '')

            if state_name == 'AnalyzeChangeset':
                phase_starts['ANALYZING'] = timestamp
                # Close INITIALIZING phase
                if 'INITIALIZING' in phase_starts and 'INITIALIZING' not in phase_times:
                    phase_times['INITIALIZING'] = int((timestamp - phase_starts['INITIALIZING']).total_seconds())

            elif 'Scan' in state_name or 'Validate' in state_name:
                phase_starts['SCANNING'] = timestamp
                # Close ANALYZING phase
                if 'ANALYZING' in phase_starts and 'ANALYZING' not in phase_times:
                    phase_times['ANALYZING'] = int((timestamp - phase_starts['ANALYZING']).total_seconds())

            elif 'Build' in state_name or state_name == 'CodeBuild':
                phase_starts['BUILDING'] = timestamp
                # Close SCANNING phase
                if 'SCANNING' in phase_starts and 'SCANNING' not in phase_times:
                    phase_times['SCANNING'] = int((timestamp - phase_starts['SCANNING']).total_seconds())

        elif event_type == 'TaskStateSucceeded':
            state_name = event.get('stateExitedEventDetails', {}).get('name', '')

            if 'Build' in state_name or state_name == 'CodeBuild':
                phase_starts['DEPLOYING'] = timestamp
                # Close BUILDING phase
                if 'BUILDING' in phase_starts and 'BUILDING' not in phase_times:
                    phase_times['BUILDING'] = int((timestamp - phase_starts['BUILDING']).total_seconds())

    # Calculate elapsed time for current phase
    if current_phase in phase_starts and current_phase not in phase_times:
        phase_times[current_phase] = int((now - phase_starts[current_phase]).total_seconds())

    return phase_times


def build_progress_message(
    deploy_id: str,
    current_phase: str,
    execution_arn: str,
) -> MessageBuilder:
    """
    Build deploy progress checklist message with elapsed times.

    Args:
        deploy_id: Deploy identifier
        current_phase: Current phase (INITIALIZING, ANALYZING, SCANNING, BUILDING, DEPLOYING)
        execution_arn: Step Functions execution ARN

    Returns:
        MessageBuilder with formatted progress checklist
    """
    mb = MessageBuilder()
    mb.text("🚀 ").bold(f"Deploy {deploy_id} 進度").newline().newline()

    # Get phase times
    phase_times = get_phase_times(execution_arn, current_phase)

    # Get emoji states for current phase
    phase_emojis = phases.get(current_phase, phases['INITIALIZING'])

    # Build checklist
    for idx, (emoji, label) in enumerate(zip(phase_emojis, phase_labels)):
        mb.text(f"{emoji} {label}")

        # Add elapsed time if available
        phase_name = list(phases.keys())[idx]
        if phase_name in phase_times:
            elapsed = phase_times[phase_name]
            time_str = format_elapsed_time(elapsed)

            if emoji == '✅':
                # Completed phase: show total time
                mb.text(f" ({time_str})")
            elif emoji == '🔄':
                # In-progress phase: show elapsed time
                mb.text(f" (已 {time_str})")

        mb.newline()

    return mb
```

### 3. Update `handle_progress` Function

**Location:** `deployer/notifier/app.py:handle_progress`

**Current signature:**
```python
def handle_progress(event, context):
    """Handle deploy progress updates from Step Functions"""
    # Extracts phase, deploy_id from event
    # Sends progress message
```

**Updated implementation:**
```python
def handle_progress(event, context):
    """
    Handle deploy progress updates from Step Functions.

    Event payload:
        {
            "deploy_id": "d-abc123",
            "phase": "BUILDING",
            "execution_arn": "arn:aws:states:us-west-2:123:execution:...",
            "project_id": "bouncer-api"
        }
    """
    deploy_id = event['deploy_id']
    phase = event['phase']
    execution_arn = event['execution_arn']
    project_id = event['project_id']

    # Build progress message with elapsed times
    mb = build_progress_message(deploy_id, phase, execution_arn)

    # Send or update message
    chat_id = get_notification_chat_id(project_id)
    message_id = get_progress_message_id(deploy_id)

    if message_id:
        # Update existing message
        update_message(chat_id=chat_id, message_id=message_id, message_builder=mb)
    else:
        # Send new message and store message_id
        result = send_message(chat_id=chat_id, message_builder=mb)
        store_progress_message_id(deploy_id, result['message_id'])

    return {'statusCode': 200}
```

### 4. Add ANALYZING Phase Trigger

**Location:** `deployer/notifier/app.py:handle_analyze`

**Current implementation:**
```python
def handle_analyze(event, context):
    """Handle changeset analysis completion"""
    # Sends analysis result notification
    # Does NOT update progress
```

**Updated implementation:**
```python
def handle_analyze(event, context):
    """
    Handle changeset analysis started/completed.

    Event payload:
        {
            "deploy_id": "d-abc123",
            "project_id": "bouncer-api",
            "execution_arn": "arn:aws:...",
            "status": "started" | "completed",
            "changeset_result": {...}  # only if status=completed
        }
    """
    deploy_id = event['deploy_id']
    status = event.get('status', 'completed')

    if status == 'started':
        # Update progress to ANALYZING phase
        handle_progress({
            'deploy_id': deploy_id,
            'phase': 'ANALYZING',
            'execution_arn': event['execution_arn'],
            'project_id': event['project_id'],
        }, context)

    elif status == 'completed':
        # Send analysis result (existing logic)
        changeset_result = event.get('changeset_result', {})
        # ... existing notification logic ...

        # Update progress to next phase
        has_infra_changes = changeset_result.get('has_infra_changes', False)
        next_phase = 'SCANNING' if not has_infra_changes else 'SCANNING'  # Always SCANNING after analyze

        handle_progress({
            'deploy_id': deploy_id,
            'phase': next_phase,
            'execution_arn': event['execution_arn'],
            'project_id': event['project_id'],
        }, context)

    return {'statusCode': 200}
```

### 5. Update Step Functions Workflow

**Location:** `deployer/template.yaml` (Step Functions definition)

**Change:** Add task state callback to trigger `handle_analyze` when AnalyzeChangeset starts

```yaml
AnalyzeChangeset:
  Type: Task
  Resource: arn:aws:states:::lambda:invoke
  Parameters:
    FunctionName: ${AnalyzeChangesetFunction}
    Payload:
      deploy_id.$: $.deploy_id
      project_id.$: $.project_id
      # ... other params ...
  ResultPath: $.changeset_result
  # NEW: Add notification callback before analysis
  Catch:
    - ErrorEquals: ["States.ALL"]
      Next: HandleFailure
  Next: CheckInfraChanges

# ADD new state before AnalyzeChangeset
NotifyAnalyzeStart:
  Type: Task
  Resource: arn:aws:states:::lambda:invoke
  Parameters:
    FunctionName: ${NotifierFunction}
    Payload:
      action: analyze
      status: started
      deploy_id.$: $.deploy_id
      project_id.$: $.project_id
      execution_arn.$: $$.Execution.Id
  ResultPath: null  # Don't modify state
  Next: AnalyzeChangeset
```

---

## Test Strategy

### Unit Tests

**File:** `tests/notifier/test_progress.py`

```python
def test_format_elapsed_time():
    """Test elapsed time formatting"""
    assert format_elapsed_time(15) == "15s"
    assert format_elapsed_time(60) == "1m"
    assert format_elapsed_time(90) == "1m 30s"
    assert format_elapsed_time(3661) == "61m 1s"

def test_get_phase_times_with_analyzing():
    """Test phase time extraction with ANALYZING phase"""
    # Mock SFN history with ANALYZING state
    # Assert phase_times includes {'INITIALIZING': 2, 'ANALYZING': 5}

def test_build_progress_message_analyzing_phase():
    """Test progress message shows ANALYZING with elapsed time"""
    mb = build_progress_message(
        deploy_id='d-123',
        current_phase='ANALYZING',
        execution_arn='arn:...'
    )
    message_text = mb.to_text()
    assert '✅ 初始化' in message_text
    assert '🔄 Changeset 分析' in message_text
    assert '⏳ Template 掃描' in message_text

def test_build_progress_message_completed_phases_show_time():
    """Test completed phases show total time"""
    mb = build_progress_message(
        deploy_id='d-123',
        current_phase='BUILDING',
        execution_arn='arn:...'
    )
    message_text = mb.to_text()
    # Should contain patterns like "✅ 初始化 (2s)"
    assert '✅ 初始化 (' in message_text or '✅ 初始化' in message_text

def test_build_progress_message_current_phase_shows_elapsed():
    """Test current phase shows 'already Xs'"""
    mb = build_progress_message(
        deploy_id='d-123',
        current_phase='BUILDING',
        execution_arn='arn:...'
    )
    message_text = mb.to_text()
    # Should contain "🔄 sam build (已 Xs)"
    assert '🔄 sam build' in message_text
```

### Integration Tests

**File:** `tests/integration/test_deploy_progress.py`

```python
def test_deploy_flow_triggers_analyzing_phase(mock_sfn, mock_telegram):
    """Test deploy flow updates progress to ANALYZING"""
    # Trigger deploy
    # Assert NotifyAnalyzeStart state called
    # Assert Telegram message updated with ANALYZING phase

def test_analyzing_phase_shows_elapsed_time(mock_sfn, mock_telegram):
    """Test ANALYZING phase displays elapsed time"""
    # Start deploy
    # Wait 3 seconds
    # Trigger progress update
    # Assert message shows "🔄 Changeset 分析 (已 3s)"

def test_full_deploy_shows_all_five_phases(mock_sfn, mock_telegram):
    """Test complete deploy flow shows all 5 phases"""
    # Run full deploy
    # Assert final message shows:
    # ✅ 初始化 (Xs)
    # ✅ Changeset 分析 (Xs)
    # ✅ Template 掃描 (Xs)
    # ✅ sam build (Xs)
    # ✅ sam deploy (Xs)
```

### Manual Testing Checklist

- [ ] Trigger deploy, verify progress message shows ANALYZING phase
- [ ] Wait 10+ seconds during analysis, verify "已 Xs" counter updates
- [ ] Complete deploy, verify all 5 phases show elapsed times
- [ ] Trigger fast deploy (< 5s per phase), verify times display correctly
- [ ] Trigger slow deploy (> 60s build), verify "Xm Ys" format
- [ ] Trigger deploy that fails during ANALYZING, verify phase state preserved
- [ ] Compare old vs new progress messages side-by-side

---

## Migration Plan

### Backward Compatibility

**Concern:** Existing in-flight deploys might break if progress handler expects 5 phases but SFN sends 4-phase events.

**Solution:**
- `handle_progress` should handle both 4-phase and 5-phase gracefully
- If phase not in `phases` dict, default to INITIALIZING
- Old deploys won't show ANALYZING phase, but won't error

**Implementation:**
```python
# Fallback for unknown phases
if current_phase not in phases:
    logger.warning(f"Unknown phase: {current_phase}, defaulting to INITIALIZING")
    current_phase = 'INITIALIZING'
```

### Rollout Steps

1. Deploy notifier Lambda with 5-phase support (backward compatible)
2. Deploy SFN workflow with NotifyAnalyzeStart state
3. Test new deploys show ANALYZING phase
4. Monitor CloudWatch logs for errors
5. Rollback plan: revert SFN workflow to remove NotifyAnalyzeStart

---

## Performance Considerations

**SFN API Calls:**
- `get_phase_times` calls `DescribeExecution` and `GetExecutionHistory` on every progress update
- History can be large (100+ events for long deploys)

**Optimization:**
- Cache execution history in Lambda memory (keyed by execution_arn)
- Only fetch new events using `nextToken` pagination
- TTL: 5 minutes (deploys rarely exceed this)

**Implementation:**
```python
# Global cache
_execution_history_cache = {}

def get_phase_times(execution_arn: str, current_phase: str) -> dict[str, int]:
    # Check cache first
    if execution_arn in _execution_history_cache:
        cached_times, cache_time = _execution_history_cache[execution_arn]
        if time.time() - cache_time < 300:  # 5min TTL
            return cached_times

    # Fetch from SFN
    phase_times = _fetch_phase_times_from_sfn(execution_arn, current_phase)

    # Update cache
    _execution_history_cache[execution_arn] = (phase_times, time.time())

    return phase_times
```

---

## Related Issues

- Issue #43: Deploy progress visualization (this implements it)
- Spec s39-001: Code-only diff summary (separate notification enhancement)
- Previous: 4-phase checklist implementation

---

## Open Questions

1. **Should ANALYZING phase always appear, even for code-only deploys?**
   - Answer: Yes, changeset analysis happens for all deploys
   - If skipped (infra auto-approved), phase shows ✅ immediately

2. **What if execution history API call fails?**
   - Fallback: show phases without elapsed times
   - Log error, continue with basic progress display

3. **Should we show estimated remaining time?**
   - Out of scope for this sprint
   - Future enhancement: use historical deploy times for estimates
