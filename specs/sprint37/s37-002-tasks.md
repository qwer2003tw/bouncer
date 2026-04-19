# s37-002 Tasks — feat: 審批通知顯示 CFN changeset 明細

**Issue:** #123
**TCS 評分：** D3（中等複雜，需要新 helper 函數 + 格式化邏輯 + 多個測試）

---

## TCS 評分說明

| 維度 | 分數 | 說明 |
|------|------|------|
| 複雜度 | D3 | 2 個新 helper 函數，1 個資料流改動（analyze → approval request） |
| 風險 | 中低 | SFN payload 加欄位（向下相容）；通知文字擴展 |
| 影響範圍 | 中 | `handle_analyze` return + `handle_infra_approval_request` 通知 |
| 測試需求 | 中等 | 需測試 helper 函數 + 整合路徑 |

---

## 實作步驟

### Step 1: 新增 `_build_resource_summary()` helper

**File:** `deployer/notifier/app.py`

在 `handle_analyze()` 上方（約 line 240）加入新函數：

```python
_MAX_SUMMARY_ITEMS = 10  # SFN payload size protection

def _build_resource_summary(resource_changes: list) -> list:
    """Extract minimal fields from CFN Changes[] for SFN payload.
    Only keeps: logical_id, action, resource_type, replacement.
    """
    summary = []
    for change in resource_changes[:_MAX_SUMMARY_ITEMS]:
        rc = change.get('ResourceChange', {})
        summary.append({
            'logical_id': rc.get('LogicalResourceId', '?')[:40],
            'action': rc.get('Action', '?'),
            'resource_type': rc.get('ResourceType', '?'),
            'replacement': rc.get('Replacement', 'False'),
        })
    return summary
```

### Step 2: 更新 `handle_analyze()` 回傳值

**File:** `deployer/notifier/app.py`，`handle_analyze()` success return（約 line 309-316）

加入：
```python
'resource_changes_summary': _build_resource_summary(analysis.resource_changes),
'has_more_changes': len(analysis.resource_changes) > _MAX_SUMMARY_ITEMS,
'total_change_count': len(analysis.resource_changes),
```

**注意：** Exception path 和 missing config path 的 return dict 保持不變（空 `resource_changes`，無需加 summary）。

### Step 3: 新增 `_format_change_detail()` helper

**File:** `deployer/notifier/app.py`

在 `handle_infra_approval_request()` 上方加入：

```python
_ACTION_EMOJI = {'Add': '➕', 'Modify': '✏️', 'Delete': '🗑️'}
_REPLACEMENT_FLAG = {
    'True': ' ⚠️ 需替換',
    'Conditional': ' ⚠️ 可能替換',
    'False': '',
}

def _format_change_detail(summary: list, has_more: bool, total: int) -> str:
    """Format resource_changes_summary for Telegram Markdown notification."""
    if not summary:
        return ''
    lines = ['📋 *變更明細：*']
    for item in summary:
        emoji = _ACTION_EMOJI.get(item.get('action', '?'), '❓')
        lid = item.get('logical_id', '?')
        rtype = item.get('resource_type', '?')
        repl = _REPLACEMENT_FLAG.get(item.get('replacement', 'False'), '')
        lines.append(f"{emoji} `{lid}` \\({rtype}\\){repl}")
    if has_more:
        remaining = total - len(summary)
        lines.append(f"\\.\\.\\. 及 {remaining} 個其他變更")
    return '\n'.join(lines)
```

### Step 4: 更新 `handle_infra_approval_request()` 通知文字

**File:** `deployer/notifier/app.py`，`handle_infra_approval_request()`（約 line 378+）

讀取新欄位並嵌入通知：
```python
resource_changes_summary = event.get('resource_changes_summary', [])
has_more = event.get('has_more_changes', False)
total = event.get('total_change_count', change_count)

detail_block = _format_change_detail(resource_changes_summary, has_more, total)
detail_section = f"\n\n{detail_block}" if detail_block else ""
```

更新 `text` 字串加入 `{detail_section}`。

### Step 5: 新增 Unit Tests

**File:** `deployer/tests/test_notifier_analyze.py`

新增 class `TestResourceChangeSummary`：
- `test_build_summary_extracts_fields`
- `test_build_summary_truncates_at_10`
- `test_build_summary_empty_input`
- `test_build_summary_truncates_logical_id`

新增 class `TestFormatChangeDetail`：
- `test_format_add_action`
- `test_format_modify_with_replacement_true`
- `test_format_modify_with_replacement_conditional`
- `test_format_delete_action`
- `test_format_has_more`
- `test_format_empty_summary_returns_empty`

新增到 `TestHandleInfraApprovalRequest`：
- `test_shows_detail_block_when_summary_present`
- `test_backward_compat_no_summary_key`

### Step 6: 跑測試

```bash
cd /home/ec2-user/projects/bouncer/deployer
python -m pytest tests/test_notifier_analyze.py -v
```

### Step 7: Code Review Checklist

- [ ] SFN payload 只加摘要，不加 `Details[]`（防大 payload）
- [ ] `resource_changes_summary` key 缺失時向下相容
- [ ] Telegram Markdown escape 正確（`(`, `)`, `.` 需要 `\` escape in MarkdownV2）
- [ ] `parse_mode` 確認（現有 notifier 用 Markdown，非 MarkdownV2 — 需確認 escape 規則）
- [ ] 超過 10 個資源時有截斷說明
