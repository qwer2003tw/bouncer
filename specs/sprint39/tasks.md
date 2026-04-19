# Sprint 39 Tasks

## Mission Control Setup

### Initialize Tasks

```bash
SCRIPTS=/home/ec2-user/.openclaw/workspace/skills/mission-control/scripts

# Task 1: Code-only deploy diff summary
bash $SCRIPTS/mc-update.sh start bouncer-s39-001 \
  "Add git diff/changeset summary to code-only auto-approve notifications" \
  "feat/code-only-diff-summary-s39" \
  "bouncer"

# Task 2: Deploy progress checklist enhancements
bash $SCRIPTS/mc-update.sh start bouncer-s39-002 \
  "Add ANALYZING phase and elapsed time to deploy progress checklist" \
  "feat/deploy-checklist-s39" \
  "bouncer"

# Task 3: Telegram date_time MessageEntity
bash $SCRIPTS/mc-update.sh start bouncer-s39-003 \
  "Use date_time MessageEntity for approval expiry timestamps" \
  "feat/datetime-entity-s39" \
  "bouncer"
```

---

## Task Breakdown

### bouncer-s39-001: Code-Only Deploy Diff Summary

**Branch:** `feat/code-only-diff-summary-s39`
**Story Points:** 3
**Dependencies:** None

**Files to modify:**
- `src/notifications.py:934` - Update `send_auto_approve_deploy_notification` signature
- `src/deployer.py:987` - Pass `diff_result.diff_summary` to notification
- `src/deployer.py:1060` - Pass formatted changeset summary to notification
- `src/deployer.py` (new) - Add `format_changeset_summary()` helper function
- `tests/test_notifications.py` - Unit tests for notification changes
- `tests/test_deployer.py` - Unit tests for `format_changeset_summary`
- `tests/integration/test_deploy_flow.py` - Integration tests

**Acceptance criteria:**
- [ ] `send_auto_approve_deploy_notification` accepts `changes_summary` parameter
- [ ] Auto-approve notifications show git diff summary or changeset summary
- [ ] Notification displays `📋 *變更：* {summary}` when changes present
- [ ] Notification displays `_(無變更明細)_` when no changes
- [ ] All tests passing

**Estimated duration:** 2-3 hours

---

### bouncer-s39-002: Deploy Progress Checklist Enhancements

**Branch:** `feat/deploy-checklist-s39`
**Story Points:** 5
**Dependencies:** None

**Files to modify:**
- `deployer/notifier/app.py:handle_progress` - Add 5th phase (ANALYZING)
- `deployer/notifier/app.py` (new) - Add `format_elapsed_time()` helper
- `deployer/notifier/app.py` (new) - Add `get_phase_times()` helper
- `deployer/notifier/app.py` (new) - Add `build_progress_message()` function
- `deployer/notifier/app.py:handle_analyze` - Trigger ANALYZING phase progress update
- `deployer/template.yaml` - Add NotifyAnalyzeStart state before AnalyzeChangeset
- `tests/notifier/test_progress.py` - Unit tests for new helpers
- `tests/integration/test_deploy_progress.py` - Integration tests

**Acceptance criteria:**
- [ ] Progress checklist shows 5 phases: 初始化 → Changeset 分析 → Template 掃描 → sam build → sam deploy
- [ ] Completed phases show total elapsed time: `✅ 初始化 (2s)`
- [ ] In-progress phase shows current elapsed time: `🔄 sam build (已 42s)`
- [ ] Time format handles minutes: `1m 30s` for >= 60s
- [ ] ANALYZING phase triggered when AnalyzeChangeset starts
- [ ] Backward compatible with existing deploys
- [ ] All tests passing

**Estimated duration:** 4-6 hours

---

### bouncer-s39-003: Telegram date_time MessageEntity

**Branch:** `feat/datetime-entity-s39`
**Story Points:** 3
**Dependencies:** None

**Files to modify:**
- `src/message_builder.py` - Add `datetime()` method
- `src/notifications.py:send_approval_request` - Use date_time entity for expires_at
- `src/notifications.py:send_approval_result` - Add timestamp with date_time entity
- `src/notifications.py:send_deploy_success` - Add completed_at with date_time entity
- `tests/test_message_builder.py` - Unit tests for `datetime()` method
- `tests/test_notifications.py` - Unit tests for updated notifications
- `tests/integration/test_telegram_entities.py` - Integration test with real Telegram API

**Acceptance criteria:**
- [ ] `MessageBuilder.datetime()` method creates correct date_time entity
- [ ] Approval request shows: `⏰ 過期時間：<date_time> (相對時間)`
- [ ] Approval result shows timestamp with date_time entity
- [ ] Deploy success shows completion time with date_time entity
- [ ] Entity offsets calculated correctly when mixed with other entities
- [ ] Telegram API accepts messages without error
- [ ] All tests passing

**Estimated duration:** 3-4 hours

---

## Testing Strategy

### Unit Tests
Run after each task implementation:
```bash
# In bouncer repo
cd /home/ec2-user/projects/bouncer

# Run specific test files
pytest tests/test_notifications.py -v
pytest tests/test_deployer.py -v
pytest tests/test_message_builder.py -v
pytest tests/notifier/test_progress.py -v
```

### Integration Tests
Run after all tasks complete:
```bash
# Full integration suite
pytest tests/integration/ -v

# Specific flows
pytest tests/integration/test_deploy_flow.py::test_auto_approve_includes_diff_summary -v
pytest tests/integration/test_deploy_progress.py::test_deploy_flow_triggers_analyzing_phase -v
pytest tests/integration/test_telegram_entities.py -v
```

### Manual Testing

**Setup test environment:**
```bash
# Deploy to staging
cd /home/ec2-user/projects/bouncer
sam build
sam deploy --config-env staging
```

**Test scenarios:**

1. **s39-001: Code-only diff summary**
   - Trigger deploy with code-only changes
   - Verify notification shows git diff summary
   - Trigger deploy via auto_approve_code_only path
   - Verify notification shows changeset summary

2. **s39-002: Deploy progress**
   - Trigger deploy, watch progress updates
   - Verify ANALYZING phase appears
   - Verify elapsed times update correctly
   - Let deploy complete, verify all 5 phases show times

3. **s39-003: date_time entity**
   - Request approval, verify absolute time displayed
   - Check in Telegram client (different timezones if possible)
   - Approve request, verify result shows timestamp
   - Complete deploy, verify success shows completion time

---

## Rollout Plan

### Phase 1: Development
- Implement tasks in parallel (can be done by different devs)
- Each task has separate branch
- Run unit tests continuously

### Phase 2: Integration
- Merge branches to integration branch: `integration/sprint39`
- Run full test suite
- Fix any conflicts or integration issues

### Phase 3: Staging Deployment
- Deploy to staging environment
- Run manual testing checklist
- Monitor CloudWatch logs for errors
- Test with real Telegram notifications

### Phase 4: Production Deployment
- Merge to main branch
- Deploy to production
- Monitor first 10 deploys closely
- Rollback plan: revert main to previous commit

---

## Risk Mitigation

### s39-001: Code-only diff summary
**Risk:** Long changeset summaries clutter notification
**Mitigation:** Truncate summary to 200 chars, add "..." if truncated

### s39-002: Deploy progress
**Risk:** SFN API calls slow down progress updates
**Mitigation:** Cache execution history with 5min TTL

### s39-003: date_time entity
**Risk:** Older Telegram clients don't support entity
**Mitigation:** Graceful degradation - shows ISO 8601 text

---

## Success Metrics

### Functional Metrics
- [ ] All 3 tasks pass unit tests
- [ ] All 3 tasks pass integration tests
- [ ] Manual testing checklist 100% complete
- [ ] Zero production errors in first 24 hours

### UX Metrics (post-deployment)
- Reduced approval response time (users see absolute expiry)
- Fewer questions about "what changed?" (diff summary visible)
- Better deploy time estimation (historical phase times)

---

## Sprint Retrospective Notes

**What went well:**
- (To be filled post-sprint)

**What could improve:**
- (To be filled post-sprint)

**Action items:**
- (To be filled post-sprint)
