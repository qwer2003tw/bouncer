# Spec: Localize Telegram Button Labels to English

**Task ID**: bouncer-s38-002
**Priority**: P1
**GitHub Issue**: #46
**Branch**: `feat/button-labels-english-s38`

---

## Feature Summary

Localize all Telegram inline keyboard button labels from Chinese to English for international accessibility and consistency with existing English labels.

### Current State Analysis

**Finding**: All inline keyboard button labels in `src/notifications.py` are **already in English**.

Audit of all button text (line-by-line review):
- Line 269: `'⚠️ Confirm'` ✅ English
- Line 270: `'❌ Reject'` ✅ English
- Line 278: `'✅ Approve'` ✅ English
- Line 279: `'🔓 Trust 10min'` ✅ English
- Line 280: `'❌ Reject'` ✅ English
- Line 321: `'✅ Approve'` ✅ English
- Line 322: `'❌ Reject'` ✅ English
- Line 391: `'🛑 End Trust'` ✅ English
- Line 474: `'✅ Approve All'` ✅ English
- Line 478: `'✅ Approve Safe Only'` ✅ English
- Line 481: `'❌ Reject'` ✅ English
- Line 562: `'🛑 Revoke Grant'` ✅ English
- Line 639: `'🛑 End Trust'` ✅ English
- Line 700: `'📁 Approve Upload'` ✅ English
- Line 701: `'❌ Reject'` ✅ English
- Line 704: `'🔓 Approve + Trust 10min'` ✅ English
- Line 912: `'✅ Approve Deploy'` ✅ English
- Line 913: `'❌ Reject'` ✅ English

**Conclusion**: All button labels are already in English.

---

## Issue Clarification Required

### Possible Interpretations

**Option A**: Issue #46 is **already resolved** in a previous sprint
- All buttons were localized to English before Sprint 38
- Task can be closed as "already complete"

**Option B**: Issue #46 refers to **message body text** (not button labels)
- Notification message bodies contain extensive Chinese text:
  - "來源：" (Source)
  - "任務：" (Task)
  - "帳號：" (Account)
  - "命令：" (Command)
  - "原因：" (Reason)
  - "執行請求" (Execution Request)
  - "批次權限申請" (Batch Permission Request)
  - etc.
- If this is the scope, issue title is misleading ("button labels")

**Option C**: Issue #46 refers to **specific flows not yet audited**
- Possible locations with Chinese labels:
  - `src/deployer.py` (SAM deployer notifications)
  - `src/callbacks.py` (callback response messages)
  - Other notification functions outside `notifications.py`

---

## Recommended Action Plan

### Phase 1: Scope Verification (Sprint 38 — Pre-Implementation)

**Tasks**:
1. Audit all Telegram-related files for Chinese text:
   ```bash
   cd /home/ec2-user/projects/bouncer
   grep -rn "[\u4e00-\u9fff]" src/ --include="*.py" | grep -i "text\|button\|keyboard"
   ```
2. Review GitHub issue #46 comments for clarification
3. Confirm scope with product owner:
   - **Buttons only** → Close as resolved
   - **Message bodies** → Retitle issue & expand scope
   - **Specific flows** → Audit deployer/callbacks

### Phase 2: Implementation (if scope confirmed)

**If scope = message bodies**:
- Create language file: `src/i18n/en.py` (English strings)
- Refactor `notifications.py` to use language strings
- Add config flag: `NOTIFICATION_LANGUAGE=en` (default English, support Chinese for legacy)

**If scope = specific flows**:
- Audit identified locations (deployer, callbacks)
- Replace Chinese strings with English equivalents
- Update tests to verify button text

---

## Acceptance Scenarios (Assuming Message Body Localization)

### Scenario 1: English Approval Request Notification

**Given**:
- System language is set to English (default)
- Agent requests command approval

**When**:
- Telegram notification is sent

**Then**:
- Message contains:
  - "Source:" instead of "來源："
  - "Account:" instead of "帳號："
  - "Command:" instead of "命令："
  - "Reason:" instead of "原因："

### Scenario 2: Button Labels Remain English

**Given**:
- Any notification with inline keyboard

**When**:
- Notification is rendered in Telegram

**Then**:
- Buttons display:
  - "✅ Approve" (not "批准")
  - "❌ Reject" (not "拒絕")
  - "🔓 Trust 10min" (not "信任 10 分鐘")

---

## Before/After Comparison (If Message Body Scope)

| Location | Before (Chinese) | After (English) |
|----------|-----------------|-----------------|
| notifications.py:203 | `"🤖 來源： {source}"` | `"🤖 Source: {source}"` |
| notifications.py:205 | `"📝 任務： {context}"` | `"📝 Task: {context}"` |
| notifications.py:210 | `"🏦 帳號："` | `"🏦 Account:"` |
| notifications.py:217 | `"📋 命令："` | `"📋 Command:"` |
| notifications.py:221 | `"💬 原因："` | `"💬 Reason:"` |
| notifications.py:260 | `"🆔 ID："` | `"🆔 ID:"` |
| notifications.py:261 | `"⏰ {timeout}後過期"` | `"⏰ Expires in {timeout}"` |
| ... | (300+ Chinese strings) | (English equivalents) |

---

## Edge Cases

### EC1: Emoji Compatibility
- **Scenario**: Some clients don't render emoji correctly
- **Expected**: Text remains readable (e.g., "Source: ztp-files" even if 🤖 doesn't render)

### EC2: Mixed-Language Context
- **Scenario**: User-provided `reason` field contains Chinese text
- **Expected**: System labels are English, user content preserves original language

---

## Test Strategy

### Unit Tests (If Message Body Scope)

**File**: `tests/test_notifications_i18n.py`

1. **Test: English notification labels**
   - Call `send_approval_request()` with `NOTIFICATION_LANGUAGE=en`
   - Assert: Message contains "Source:", "Account:", "Command:"

2. **Test: Button labels unchanged**
   - Parse `inline_keyboard` from notification
   - Assert: Button text is "✅ Approve", "❌ Reject"

### Manual Tests

1. **Test: Telegram render verification**
   - Send test notification to Telegram
   - Verify: Emoji + English text displays correctly
   - Verify: No layout issues (long English words vs. Chinese characters)

---

## Migration Notes

### TOOLS.md Update (If Scope Confirmed)

**No changes required** — button labels are internal implementation details, not exposed in MCP tool interface.

---

## Open Questions

1. **What is the actual scope of issue #46?**
   - **Answer**: Requires product owner clarification
   - **Impact**: Determines if task is 1 hour (close as resolved) vs. 2 weeks (full i18n)

2. **Should we support bilingual notifications?**
   - **Option A**: English-only (simplifies maintenance)
   - **Option B**: Config-driven (English/Chinese) for backwards compatibility
   - **Recommendation**: English-only (Telegram audience is international)

3. **Should we localize log messages?**
   - **Current**: Python logs use English
   - **Decision**: No — logs are for engineers (English is standard)

---

## Implementation Estimate (Contingent on Scope)

**If scope = buttons only**: **0 hours** (already complete)
**If scope = message bodies**: **8–12 hours** (i18n refactor + testing)
**If scope = specific flows**: **4–6 hours** (audit + localization)

---

## References

- **GitHub Issue**: #46 (Localize Telegram button labels → English)
- **Related Code**: `src/notifications.py`, `src/telegram.py`
- **Audit Date**: 2026-03-14
