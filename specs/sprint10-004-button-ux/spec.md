# Sprint 10-004: feat: 按鈕英文 + style 顏色

> GitHub Issues: #46 (英文), #41 (style 顏色)
> Priority: P1
> Generated: 2026-03-03

---

## Feature Name

Button UX Improvement — 所有 Telegram inline keyboard 按鈕文字改為英文 + 加入 Bot API 9.4 的 `style` 欄位（Approve=綠, Reject=紅, Trust=藍）。

## Change Scope

### #46: 按鈕文字英文化

| 中文 | 英文 |
|------|------|
| ⚠️ 確認執行 | ⚠️ Confirm |
| ✅ 批准 | ✅ Approve |
| ✅ 批准部署 | ✅ Approve Deploy |
| 🔓 信任10分鐘 | 🔓 Trust 10min |
| ❌ 拒絕 | ❌ Reject |
| 🛑 結束信任 | 🛑 End Trust |
| ✅ 全部批准 | ✅ Approve All |
| ✅ 只批准安全的 | ✅ Approve Safe Only |
| 🛑 撤銷 Grant | 🛑 Revoke Grant |
| 📁 批准上傳 | 📁 Approve Upload |
| 🔓 批准 + 信任10分鐘 | 🔓 Approve + Trust 10min |

### #41: 按鈕 style 顏色

依 Bot API 9.4 `InlineKeyboardButton.style` 欄位：

| 按鈕類型 | style 值 |
|----------|----------|
| Approve/Confirm/Approve Deploy/Approve All/Approve Safe Only/Approve Upload | `positive` |
| Reject | `destructive` |
| Trust 10min/Approve + Trust 10min | `secondary` |
| End Trust/Revoke Grant | `destructive` |

## User Stories

**US-1: 英文按鈕**
As **Steven**,
I want all Bouncer notification buttons in English,
So that the interface is consistent with the English technical context.

**US-2: 顏色區分**
As **Steven**,
I want approve buttons in green, reject in red, and trust in blue,
So that I can quickly identify the action type at a glance.

## Acceptance Scenarios

### Scenario 1: 一般命令審批通知
- **Given**: 非高危命令的審批通知
- **When**: Telegram 發送通知
- **Then**: 按鈕顯示 `✅ Approve` / `🔓 Trust 10min` / `❌ Reject`
- **And**: Approve = positive（綠）, Trust = secondary（藍）, Reject = destructive（紅）

### Scenario 2: 高危命令審批通知
- **Given**: 高危命令（dangerous=True）
- **When**: Telegram 發送通知
- **Then**: 按鈕顯示 `⚠️ Confirm` / `❌ Reject`
- **And**: Confirm = positive, Reject = destructive

### Scenario 3: 部署審批通知
- **Given**: Deploy 請求
- **When**: Telegram 發送通知
- **Then**: 按鈕顯示 `✅ Approve Deploy` / `❌ Reject`

### Scenario 4: Grant session 通知
- **Given**: Grant session 請求
- **When**: Telegram 發送通知
- **Then**: `✅ Approve All` / `✅ Approve Safe Only` / `❌ Reject`

### Scenario 5: Trust/Grant revoke 通知
- **Given**: Active trust/grant session 通知
- **When**: 按鈕顯示
- **Then**: `🛑 End Trust` / `🛑 Revoke Grant` = destructive（紅）

## Requirements

- **R1**: 所有按鈕 `text` 改為英文（對照表如上）
- **R2**: 所有按鈕加入 `style` 欄位
- **R3**: `callback_data` 不變（不影響 callback handler）
- **R4**: Bot API 9.4+ 才支援 style — 舊版客戶端會忽略此欄位，不影響功能

## Interface Contract

### 按鈕 JSON 格式

之前：
```json
{"text": "✅ 批准", "callback_data": "approve:xxx"}
```

之後：
```json
{"text": "✅ Approve", "callback_data": "approve:xxx", "style": "positive"}
```

### style 值對照（Bot API 9.4）

| style 值 | 顏色 | 用途 |
|-----------|------|------|
| `positive` | 綠 | Approve 類 |
| `destructive` | 紅 | Reject / Revoke 類 |
| `secondary` | 藍/灰 | Trust 類 |

> 注：Bot API 文件的 style 實際值需確認。若 API 用 `success`/`danger`/`primary` 則對應調整。
