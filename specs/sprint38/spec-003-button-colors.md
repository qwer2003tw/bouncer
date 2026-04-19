# Spec: Inline Keyboard Button Visual Distinction

**Task ID**: bouncer-s38-003
**Priority**: P1
**GitHub Issue**: #41
**Branch**: `feat/button-colors-s38`

---

## Feature Summary

Improve visual distinction of Telegram inline keyboard buttons using color-coded emoji prefixes (🟢 green for approve, 🔴 red for deny, 🔵 blue for trust) to replace current action-based emoji (✅ ❌ 🔓).

### Problem Statement

**Current implementation**:
- Buttons use action-based emoji: ✅ (checkmark), ❌ (X mark), 🔓 (unlock)
- Code includes `'style': 'success' | 'danger' | 'primary'` attributes
- Telegram Bot API **does not support button colors natively**
- `style` attribute is stripped by `_strip_unsupported_button_fields()` (telegram.py:167-174)

**User experience issue**:
- In high-pressure approval scenarios, buttons don't visually distinguish "safe" vs. "dangerous" actions
- Current emoji (✅ ❌) are semantic but not color-coded
- Risk of accidental approve/deny due to lack of visual separation

---

## Telegram API Limitation Analysis

### Native Button Color Support: ❌ Not Available

**Telegram Bot API** inline keyboard specification:
- Buttons support: `text`, `callback_data`, `url`, `login_url`, `switch_inline_query`
- **No color/style parameter** in official API (v7.0, latest as of 2026-03)

**Attempted Workarounds (Rejected)**:
1. **CSS/HTML styling**: Not supported in Telegram messages (plain text + entities only)
2. **Custom keyboard markup**: Not applicable to inline keyboards (only reply keyboards)
3. **Themed buttons**: No theme customization in Bot API

**Reference**: [Telegram Bot API InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton)

### Code Audit: Current `style` Attribute

**Location**: `src/notifications.py`

All button definitions include unused `style` attribute:
```python
{'text': '✅ Approve', 'callback_data': 'approve:...', 'style': 'success'}
{'text': '❌ Reject', 'callback_data': 'deny:...', 'style': 'danger'}
{'text': '🔓 Trust 10min', 'callback_data': 'approve_trust:...', 'style': 'primary'}
```

**Stripping logic** (`src/telegram.py:167-174`):
```python
def _strip_unsupported_button_fields(result: dict) -> dict:
    """Remove fields Telegram doesn't support (e.g., 'style')"""
    if 'inline_keyboard' in result:
        result['inline_keyboard'] = [
            [{k: v for k, v in btn.items() if k in ['text', 'callback_data', 'url', ...]}
             for btn in row]
            for row in result['inline_keyboard']
        ]
    return result
```

**Impact**: `style` is documentation-only (stripped before sending to Telegram)

---

## Solution: Color-Coded Emoji Prefixes

### Approach: Replace Semantic Emoji with Color Emoji

**Design Decision**: Use **colored circle emoji** (🟢 🔴 🔵) as visual color cues

| Action Type | Current Emoji | New Emoji | Color Semantic |
|------------|---------------|-----------|----------------|
| Approve | ✅ Checkmark | 🟢 Green Circle | Safe / Proceed |
| Deny/Reject | ❌ X Mark | 🔴 Red Circle | Danger / Stop |
| Trust | 🔓 Unlock | 🔵 Blue Circle | Trust / Elevated |

**Rationale**:
- **Colored circles** are universally recognized (traffic light pattern: 🟢 go, 🔴 stop)
- **Higher contrast** than semantic emoji (✅ vs. 🟢)
- **Accessibility**: Color + text label (redundant encoding for colorblind users)
- **Consistent with user request** (#41 explicitly mentions "green, red, blue")

### Alternative Approaches (Considered & Rejected)

**Option A**: Keep semantic emoji (✅ ❌ 🔓)
- ❌ Rejected: Does not address visual distinction issue
- ❌ User feedback (#41) indicates current emoji insufficient

**Option B**: Use filled squares (🟩 🟥 🟦)
- ❌ Rejected: Less visually distinct than circles (harder to differentiate at small size)

**Option C**: Add color words to text (`"🟢 Approve (Safe)"`)
- ❌ Rejected: Increases button width, clutters UI on mobile

**Option D**: Custom button graphics (requires web app)
- ❌ Rejected: Telegram inline keyboards are text-only, no image support

---

## User Stories

### P1: Visual Safety Cue
**As a** human approver reviewing commands
**I want** approve buttons to be green and deny buttons to be red
**So that** I can quickly identify safe vs. dangerous actions

**Acceptance Criteria**:
- Approve buttons: 🟢 Green circle prefix
- Deny/Reject buttons: 🔴 Red circle prefix
- Trust buttons: 🔵 Blue circle prefix
- All buttons retain text labels ("Approve", "Reject", "Trust 10min")

### P2: Colorblind Accessibility
**As a** colorblind approver
**I want** buttons to have both color and text labels
**So that** I can distinguish actions even if colors appear similar

**Acceptance Criteria**:
- Color emoji + text label (e.g., "🟢 Approve" not just "🟢")
- Text labels remain unchanged ("Approve", "Reject", "Trust")
- Screen readers read full text (Telegram auto-handles emoji alt text)

---

## Acceptance Scenarios

### Scenario 1: Approval Request Notification

**Given**:
- Agent requests AWS command approval (non-dangerous)

**When**:
- Telegram notification is sent

**Then**:
- Inline keyboard buttons display:
  - `🟢 Approve` (green circle + "Approve")
  - `🔵 Trust 10min` (blue circle + "Trust 10min")
  - `🔴 Reject` (red circle + "Reject")

### Scenario 2: Dangerous Command Confirmation

**Given**:
- Agent requests dangerous command (e.g., `rm -rf /`)

**When**:
- Telegram notification is sent

**Then**:
- Inline keyboard buttons display:
  - `⚠️ Confirm` (warning triangle — **no color change**, semantic override)
  - `🔴 Reject` (red circle)

**Note**: Dangerous commands use `⚠️ Confirm` instead of `🟢 Approve` to emphasize caution.

### Scenario 3: Grant Session Approval

**Given**:
- Agent requests batch grant approval

**When**:
- Telegram notification is sent

**Then**:
- Buttons display:
  - `🟢 Approve All`
  - `🟢 Approve Safe Only` (if applicable)
  - `🔴 Reject`

---

## Before/After Button Comparison

### Command Approval (Normal)

**Before**:
```
✅ Approve    🔓 Trust 10min    ❌ Reject
```

**After**:
```
🟢 Approve    🔵 Trust 10min    🔴 Reject
```

### Command Approval (Dangerous)

**Before**:
```
⚠️ Confirm    ❌ Reject
```

**After** (no change for dangerous commands):
```
⚠️ Confirm    🔴 Reject
```

### Account Management

**Before**:
```
✅ Approve    ❌ Reject
```

**After**:
```
🟢 Approve    🔴 Reject
```

### Upload Approval

**Before**:
```
📁 Approve Upload    ❌ Reject
🔓 Approve + Trust 10min
```

**After**:
```
🟢 Approve Upload    🔴 Reject
🔵 Approve + Trust 10min
```

### Trust/Grant Management

**Before**:
```
🛑 End Trust
🛑 Revoke Grant
```

**After** (no change — stop sign emoji already red-colored):
```
🛑 End Trust
🛑 Revoke Grant
```

---

## Implementation Details

### Files to Modify

**Primary**: `src/notifications.py`
- Update all button `'text'` fields with new emoji prefixes

**No changes required**:
- `src/telegram.py` (stripping logic unchanged)
- `src/callbacks.py` (button callbacks remain `approve:`, `deny:`, etc.)

### Code Changes (Line-by-Line)

**notifications.py:269** (Dangerous command confirm):
```python
# Before
{'text': '⚠️ Confirm', ...}
# After (no change — warning triangle is semantically correct)
{'text': '⚠️ Confirm', ...}
```

**notifications.py:270, 280, 322, 481, 701, 913**:
```python
# Before
{'text': '❌ Reject', ...}
# After
{'text': '🔴 Reject', ...}
```

**notifications.py:278, 321, 474, 912**:
```python
# Before
{'text': '✅ Approve', ...}
# After
{'text': '🟢 Approve', ...}
```

**notifications.py:478**:
```python
# Before
{'text': '✅ Approve Safe Only', ...}
# After
{'text': '🟢 Approve Safe Only', ...}
```

**notifications.py:279, 704**:
```python
# Before
{'text': '🔓 Trust 10min', ...}
# After
{'text': '🔵 Trust 10min', ...}
```

**notifications.py:700**:
```python
# Before
{'text': '📁 Approve Upload', ...}
# After
{'text': '🟢 Approve Upload', ...}
```

**notifications.py:391, 562, 639** (End Trust / Revoke Grant):
```python
# Before
{'text': '🛑 End Trust', ...}
# After (no change — stop sign already conveys "stop/danger")
{'text': '🛑 End Trust', ...}
```

### `style` Attribute Cleanup (Optional)

**Current state**: All buttons have unused `'style'` attribute
**Proposed**: Remove `'style'` from all button definitions (cleanup)

**Rationale**:
- Not functional (stripped by `_strip_unsupported_button_fields`)
- Misleading (implies Telegram supports colors)
- Code noise (adds 20 characters per button)

**Implementation**:
```python
# Before
{'text': '🟢 Approve', 'callback_data': 'approve:...', 'style': 'success'}

# After (cleanup)
{'text': '🟢 Approve', 'callback_data': 'approve:...'}
```

**Risk**: Low (attribute is already ignored, removal has no runtime impact)

---

## Edge Cases

### EC1: Emoji Rendering on Older Telegram Clients
- **Scenario**: User runs Telegram v5.0 (2019) which doesn't render colored circle emoji
- **Fallback**: Telegram displays fallback characters (e.g., `[GREEN CIRCLE]`)
- **Mitigation**: Text label ("Approve") remains readable
- **Decision**: Acceptable trade-off (colored circles are Unicode 12.0, widely supported since 2019)

### EC2: Colorblind User (Red-Green Deficiency)
- **Scenario**: User cannot distinguish 🟢 from 🔴
- **Fallback**: Text labels ("Approve" vs. "Reject") remain distinct
- **Design**: Approve actions typically appear leftmost, Reject rightmost (positional cue)
- **Compliance**: Meets WCAG 2.1 Level AA (don't rely on color alone)

### EC3: Dark Mode vs. Light Mode
- **Scenario**: Colored circle emoji may appear different in Telegram dark mode
- **Expected**: Emoji renders consistently across themes (system-level rendering, not Telegram-controlled)
- **Verification**: Manual test on iOS/Android dark mode

---

## Test Strategy

### Unit Tests

**File**: `tests/test_notifications_button_colors.py`

1. **Test: Approve button has green circle**
   - Call `send_approval_request(...)`
   - Parse returned `inline_keyboard`
   - Assert: Approve button text starts with `'🟢'`

2. **Test: Reject button has red circle**
   - Assert: Reject button text starts with `'🔴'`

3. **Test: Trust button has blue circle**
   - Assert: Trust button text starts with `'🔵'`

4. **Test: Dangerous command uses warning triangle**
   - Call with `command='rm -rf /'` (dangerous)
   - Assert: Confirm button text starts with `'⚠️'` (not `'🟢'`)

### Manual Tests

1. **Test: Visual rendering on mobile**
   - Send test notification to Telegram mobile app (iOS + Android)
   - Verify: Colored circles render correctly
   - Verify: No layout issues (emoji size vs. text alignment)

2. **Test: Dark mode rendering**
   - Enable Telegram dark mode
   - Send test notification
   - Verify: Colored circles remain visible (not washed out)

### Regression Tests

1. **Test: Callback functionality unchanged**
   - Click each button type (Approve, Reject, Trust)
   - Verify: Correct callback data sent (`approve:`, `deny:`, `approve_trust:`)
   - Verify: No functional regressions from emoji changes

---

## Migration Notes

### TOOLS.md Update

**No changes required** — button styling is internal implementation, not part of MCP tool interface.

### Changelog Entry

```markdown
## Sprint 38 - 2026-03-14

### Changed
- Improved Telegram button visual distinction (#41)
  - Approve buttons: 🟢 Green circle (was ✅ checkmark)
  - Deny/Reject buttons: 🔴 Red circle (was ❌ X mark)
  - Trust buttons: 🔵 Blue circle (was 🔓 unlock icon)
- No functional changes (callbacks remain identical)
```

---

## Accessibility Compliance

### WCAG 2.1 Level AA Checklist

- ✅ **1.4.1 Use of Color**: Color not sole means of conveying information (text labels present)
- ✅ **1.4.3 Contrast (Minimum)**: Emoji + text provides sufficient contrast (Telegram-controlled)
- ✅ **2.5.3 Label in Name**: Button text matches accessible name (Telegram auto-generates from text)
- ✅ **4.1.2 Name, Role, Value**: Buttons have clear semantic roles (Telegram inline keyboard spec)

---

## Open Questions

1. **Should we remove the `style` attribute entirely?**
   - **Option A**: Remove (cleanup, reduces code noise)
   - **Option B**: Keep (documents intent, no harm in leaving)
   - **Recommendation**: Remove in Sprint 39 (separate cleanup PR, lower priority)

2. **Should "End Trust" button change to red circle?**
   - **Current**: 🛑 End Trust (stop sign emoji, already red)
   - **Alternative**: 🔴 End Trust (consistent with other "danger" actions)
   - **Recommendation**: Keep 🛑 (stop sign is stronger semantic cue than circle)

3. **Should we add emoji legend to notifications?**
   - **Example**: Footer text "🟢 Safe | 🔴 Danger | 🔵 Trust"
   - **Pros**: Educates new users
   - **Cons**: Clutters every notification
   - **Recommendation**: No (color + text is self-explanatory)

---

## References

- **GitHub Issue**: #41 (Inline keyboard button color: approve=green, deny=red, trust=blue)
- **Related Code**: `src/notifications.py`, `src/telegram.py`
- **Telegram Docs**: [InlineKeyboardButton API](https://core.telegram.org/bots/api#inlinekeyboardbutton)
- **Unicode Emoji**: [Colored Circle Emoji](https://emojipedia.org/large-green-circle/) (Unicode 12.0, 2019)
