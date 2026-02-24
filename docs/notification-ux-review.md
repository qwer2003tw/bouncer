# Bouncer Telegram 通知 UX 完整審查報告

**審查日期:** 2026-02-24  
**審查範圍:** `src/notifications.py`, `src/callbacks.py`, `src/deployer.py`, `src/app.py`, `src/mcp_upload.py`, `src/paging.py`, `src/telegram_commands.py`  
**Telegram Parse Mode:** Markdown V1

---

## 目錄

1. [通知類型全覽](#1-通知類型全覽)
2. [逐一分析](#2-逐一分析)
3. [問題清單](#3-問題清單)
4. [全域一致性建議](#4-全域一致性建議)

---

## 1. 通知類型全覽

共發現 **28 個通知發送點**，歸納為以下類型：

| # | 通知類型 | 檔案 | 觸發場景 | 有按鈕 | 粗體 | 靜默 |
|---|---------|------|---------|-------|------|------|
| 1 | 命令審批請求（普通） | notifications.py:66 | MCP/REST 命令請求 | ✅ | ✅ | ❌ |
| 2 | 命令審批請求（高危） | notifications.py:52 | 危險命令請求 | ✅ | ✅ | ❌ |
| 3 | 帳號新增審批請求 | notifications.py:91 | add_account | ✅ | ✅ | ❌ |
| 4 | 帳號移除審批請求 | notifications.py:101 | remove_account | ✅ | ✅ | ❌ |
| 5 | Trust 自動批准通知 | notifications.py:117 | trust 期間自動執行 | ✅ | ✅ | ✅ |
| 6 | Grant 審批請求 | notifications.py:156 | request_grant | ✅ | ✅ | ❌ |
| 7 | Grant 自動執行通知 | notifications.py:246 | grant 期間執行命令 | ✅ | ✅ | ✅ |
| 8 | Grant 完成通知 | notifications.py:320 | grant 結束/過期 | ❌ | ✅ | ✅ |
| 9 | 命令封鎖通知 | notifications.py:337 | 命令被 block | ❌ | ✅ | ✅ |
| 10 | Trust 上傳通知 | notifications.py:363 | trust 期間自動上傳 | ✅ | ✅ | ✅ |
| 11 | 批量上傳審批請求 | notifications.py:400 | upload_batch | ✅ | ✅ | ❌ |
| 12 | 單檔上傳審批請求 | mcp_upload.py:349 | upload | ✅ | ✅ | ❌ |
| 13 | 部署審批請求 | deployer.py:504 | deploy | ✅ | ❌ | ❌ |
| 14 | 命令批准後結果 | callbacks.py:236 | approve/approve_trust | ❌ | ✅ | ❌ |
| 15 | 命令拒絕後更新 | callbacks.py:260 | deny | ❌ | ✅ | ❌ |
| 16 | 帳號新增批准 | callbacks.py:243 | approve add_account | ❌ | ✅ | ❌ |
| 17 | 帳號新增拒絕 | callbacks.py:256 | deny add_account | ❌ | ✅ | ❌ |
| 18 | 帳號移除批准 | callbacks.py:286 | approve remove_account | ❌ | ✅ | ❌ |
| 19 | 帳號移除拒絕 | callbacks.py:298 | deny remove_account | ❌ | ✅ | ❌ |
| 20 | 部署啟動成功 | callbacks.py:463 | approve deploy | ❌ | ✅ | ❌ |
| 21 | 部署啟動失敗 | callbacks.py:454 | approve deploy error | ❌ | ✅ | ❌ |
| 22 | 部署拒絕 | callbacks.py:479 | deny deploy | ❌ | ✅ | ❌ |
| 23 | 上傳批准 | callbacks.py:537 | approve upload | ❌ | ❌ | ❌ |
| 24 | 上傳失敗 | callbacks.py:547 | approve upload error | ❌ | ❌ | ❌ |
| 25 | 上傳拒絕 | callbacks.py:557 | deny upload | ❌ | ❌ | ❌ |
| 26 | 批量上傳完成 | callbacks.py:661 | approve upload_batch | ❌ | ❌ | ❌ |
| 27 | 批量上傳拒絕 | callbacks.py:673 | deny upload_batch | ❌ | ❌ | ❌ |
| 28 | Grant 批准後更新 | callbacks.py:71 | grant_approve | ❌ | ✅ | ❌ |
| 29 | Grant 拒絕後更新 | callbacks.py:112 | grant_deny | ❌ | ✅ | ❌ |
| 30 | Trust 撤銷更新 | app.py:512 | revoke_trust | ❌ | ✅ | ❌ |
| 31 | Grant 撤銷更新 | app.py:530 | grant_revoke | ❌ | ✅ | ❌ |
| 32 | 已處理請求更新 | app.py:563 | 重複點按已處理的請求 | ❌ | ✅ | ❌ |
| 33 | 已過期請求更新 | app.py:593 | 點按已過期的請求 | ❌ | ✅ | ❌ |
| 34 | 分頁剩餘頁面 | paging.py:41 | 長輸出分頁 | ❌ | ✅ | ❌ |
| 35 | /accounts 回覆 | telegram_commands.py:79 | 使用者指令 | ❌ | ❌ | ❌ |
| 36 | /trust 回覆 | telegram_commands.py:99 | 使用者指令 | ❌ | ❌ | ❌ |
| 37 | /pending 回覆 | telegram_commands.py:118 | 使用者指令 | ❌ | ❌ | ❌ |
| 38 | /help 回覆 | telegram_commands.py:131 | 使用者指令 | ❌ | ❌ | ❌ |

---

## 2. 逐一分析

### 2.1 命令審批請求 — `send_approval_request()` (notifications.py:24)

**觸發:** MCP/REST 發送 AWS CLI 命令需要審批

**格式分析:**
- 標題用 `*粗體*` ✅
- 命令用 `` ` `` inline code（截斷 500 字元）
- `reason` 已 escape ✅
- `source` 已 escape ✅
- `context` 已 escape ✅
- `build_info_lines(bold=True)` — 預設粗體 ✅
- `account_line` 手動構造，未使用 `build_info_lines` 的 account 參數 ⚠️

**按鈕:**
- 普通: `✅ 批准` / `🔓 信任10分鐘` / `❌ 拒絕`（三按鈕一排）
- 高危: `⚠️ 確認執行` / `❌ 拒絕`（兩按鈕一排）

**問題:**
1. 命令用 inline code (`` `cmd_preview` ``)，如果命令超長（500 字元）或包含換行，inline code 會顯示異常。應用 code block。
2. `account_line` 獨立於 `build_info_lines`，emoji 不同（🏢 vs 🏦），格式不一致。
3. 三個按鈕在一排可能在手機上文字被截斷。

---

### 2.2 帳號管理審批請求 — `send_account_approval_request()` (notifications.py:85)

**觸發:** add_account / remove_account

**格式分析:**
- `name` 有 escape ✅
- `source` 有 escape ✅
- `context` 有 escape ✅
- account_id 放在 inline code ✅
- role_arn 放在 inline code ✅

**問題:**
1. `account_id` 和 `role_arn` 是系統值，不太可能含 Markdown 特殊字元，但也沒 escape。低風險。
2. 新增和移除用不同文字結構（新增有 Role 行，移除沒有），合理。

---

### 2.3 Trust 自動批准通知 — `send_trust_auto_approve_notification()` (notifications.py:117)

**觸發:** Trust session 期間自動批准命令

**格式分析:**
- 命令截斷 **100 字元**（vs 審批請求的 500）
- 結果截斷 **500 字元**
- 結果用 code block (` ``` `) ✅
- `source` 放在 inline code 但**沒有 escape** ⚠️
- `cmd_preview` 放在 inline code 但沒有 escape（code block 內不需要，但這裡是 inline code）

**問題:**
1. `source` 用 `` `source` `` 包裹但**未 escape**。如果 source 含 `` ` ``，會壞。P1。
2. 結果判斷 `result.startswith('❌')` 依賴結果前綴，不太可靠。
3. `session_info` 拼接邏輯用 `·` 分隔，但如果 source 為空、remaining 非空，會有多餘空白。

---

### 2.4 Grant 審批請求 — `send_grant_request_notification()` (notifications.py:156)

**觸發:** request_grant MCP tool

**格式分析:**
- `source` **沒有 escape** ⚠️ (line 207: `source or 'Unknown'` 直接嵌入)
- `reason` **沒有 escape** ⚠️ (line 208: `reason or ''` 直接嵌入)
- 命令列表用 inline code 截斷 80 字元
- 用 max_display = 10 限制顯示數量 ✅
- 按鈕根據命令分類動態顯示 ✅

**問題:**
1. **P0: `source` 和 `reason` 沒有 escape markdown。** 如果包含 `_`（常見於 bot 名稱如 `Private_Bot`），會導致格式壞掉或 Telegram API 400 錯誤。
2. 命令列表的命令截斷 80 字元（vs 審批請求 500，Trust 通知 100），不一致。
3. `account_id` 在 inline code 中 ✅，但沒有 `account_name`（只顯示 ID，其他地方都是 `ID (Name)` 格式）。

---

### 2.5 Grant 自動執行通知 — `send_grant_execute_notification()` (notifications.py:246)

**觸發:** Grant session 期間命令自動執行

**格式分析:**
- 命令截斷 **100 字元** ✅
- 結果截斷 **200 字元**（vs Trust 的 500）⚠️
- 結果用 **inline code** (`` `result_text` ``)（vs Trust 用 code block）⚠️
- `grant_id` 截斷 20 字元
- `remaining_info` 未 escape ⚠️

**問題:**
1. **P0: 結果用 inline code**，多行結果會顯示異常。應改用 code block。
2. **P1: 結果截斷 200 字元**（Trust 用 500），不一致且太短。
3. **P1: `remaining_info` 沒有 escape。** 由程式產生，低風險但仍應 escape。

---

### 2.6 Grant 完成通知 — `send_grant_complete_notification()` (notifications.py:320)

**觸發:** Grant session 結束/過期

**問題:**
1. **P2: 此函數定義了但從未被呼叫。** 搜尋全 codebase 沒有任何 caller。dead code。
2. `reason` **沒有 escape** ⚠️。

---

### 2.7 命令封鎖通知 — `send_blocked_notification()` (notifications.py:337)

**觸發:** 命令被 blocklist 攔截

**格式分析:**
- 命令截斷 100 字元，inline code ✅
- `block_reason` **沒有 escape** ⚠️（系統產生的，風險低）
- `source` **沒有 escape** ⚠️

**問題:**
1. **P1: `source` 沒有 escape。** source 是 user input。
2. `block_reason` 是系統產生的文字，不太可能含 Markdown 字元，但仍應 escape。

---

### 2.8 Trust 上傳通知 — `send_trust_upload_notification()` (notifications.py:363)

**觸發:** Trust session 期間自動批准上傳

**格式分析:**
- `filename` 在 inline code 中 ✅
- `sha256_hash` 截斷 16 字元在 inline code 中 ✅
- `source` 在 inline code 中但**沒有 escape** ⚠️
- `trust_id` 在 inline code 中 ✅

**問題:**
1. **P1: `source` 沒有 escape。**
2. 沒有 account 資訊（信任上傳不顯示帳號，但其他通知都有）。

---

### 2.9 批量上傳審批請求 — `send_batch_upload_notification()` (notifications.py:400)

**觸發:** upload_batch MCP tool

**格式分析:**
- `source` 有 escape ✅
- `reason` 有 escape ✅
- `account_name` 有 escape ✅
- `ext_line` 未 escape（系統產生，低風險）
- `batch_id` 在 inline code ✅

**問題:**
1. `account_name` 顯示但沒有 `account_id`。其他地方都是 `ID (Name)` 格式。
2. 按鈕排列兩排合理（`📁 批准上傳` / `❌ 拒絕` + `🔓 批准 + 信任10分鐘`）。

---

### 2.10 單檔上傳審批請求 — `_submit_upload_for_approval()` (mcp_upload.py:349)

**觸發:** upload MCP tool

**格式分析:**
- `source` 有 escape ✅
- `reason` 有 escape ✅
- `s3_uri` 有 escape ✅（但 s3_uri 放在 inline code 裡，code 裡面 escape 會導致顯示 `\_` 等字元 ⚠️）
- `content_type` 有 escape ✅
- `account` 有 escape ✅

**問題:**
1. **P1: `safe_s3_uri` 先 escape 再放進 inline code `` `...` ``。** inline code 裡面不需要 escape，會導致顯示 `s3://bucket/path\_with\_underscore`。應取消對 inline code 內文字的 escape。
2. 這個審批請求直接在 `mcp_upload.py` 裡手動構造 Markdown，沒有使用 `build_info_lines()`。與 `notifications.py` 的寫法不一致。

---

### 2.11 部署審批請求 — `send_deploy_approval_request()` (deployer.py:504)

**觸發:** deploy MCP tool

**格式分析:**
- **沒有使用 `*粗體*`** ⚠️ — 所有欄位標籤都是純文字
- `source` 有 escape ✅
- `reason` 有 escape ✅
- `context` 有 escape ✅
- 手動構造 `source_line` / `context_line`，未使用 `build_info_lines()` ⚠️
- Emoji 用法不同：🤖, 📝, 📦, 🌿, 🏢, 📋, 💬, 🆔, ⏰

**問題:**
1. **P0: 整個通知沒有粗體。** 這是唯一一個審批請求通知沒用粗體的，視覺上與其他通知明顯不一致，看起來像「二等公民」。
2. **P1: 手動構造 source_line / context_line**，格式是 `🤖 來源： {source}` 而非 `🤖 *來源：* {source}`。emoji 後面沒有粗體標籤名，也沒用 `build_info_lines()`。
3. `target_account` 提取邏輯（從 role ARN 解析）放在通知函數裡，屬於 business logic。

---

### 2.12 命令批准後結果更新 — `handle_command_callback()` (callbacks.py:214–258)

**觸發:** 點按批准/信任

**格式分析:**
- 標題用粗體 ✅
- 命令顯示**完整命令**（未截斷）⚠️
- 結果用 code block ✅
- 結果截斷 800（信任模式）或 1000（普通模式）字元
- `reason` **沒有 escape** ⚠️（從 DynamoDB 讀回的原始值）
- `source` 和 `context` 透過 `build_info_lines()` 處理，但值來自 DB 未重新 escape ⚠️
- 信任時段資訊顯示 `命令: 0/20 | 上傳: 0/5` ✅

**問題:**
1. **P0: `reason` 沒有 escape。** `reason` 存入 DB 時是原始值，讀回後直接嵌入 Markdown。如果 reason 含 `_` 或 `*`，會壞掉。
2. **P1: `source` 和 `context` 從 DB 讀回後未重新 escape。** `build_info_lines()` 不會幫你 escape，它只是格式化。呼叫者需要確保值已 escape。
3. **P1: 命令未截斷。** 審批請求截斷 500，但結果更新顯示完整命令。超長命令（如 DynamoDB scan with filters）會導致訊息太長。
4. 截斷長度 800 vs 1000 的差異合理（信任模式留空間給信任資訊），但缺少統一常數。

---

### 2.13 命令拒絕後更新 (callbacks.py:260–274)

**格式分析:**
- 類似批准後更新，但沒有結果
- `reason` **沒有 escape** ⚠️（同上問題）
- `source`/`context` 透過 `build_info_lines()` 但未 escape ⚠️

---

### 2.14 部署 callback 處理 (callbacks.py:446–491)

**觸發:** 部署審批按鈕

**格式分析:**
- 批准後：`reason` 有呼叫 `escape_markdown()` ✅
- 拒絕後：`reason` **沒有 escape** ⚠️（line 489: `💬 *原因：* {reason}`）

**問題:**
1. **P1: 部署拒絕時 `reason` 沒有 escape。** 批准時有 escape，拒絕時忘了。不一致。
2. 部署拒絕的訊息有 `📋 *Stack：*` 行，但部署批准成功的訊息也有。一致 ✅。

---

### 2.15 上傳 callback 處理 (callbacks.py:510–570)

**觸發:** 上傳審批按鈕

**格式分析:**
- 使用 `build_info_lines(bold=False)` ⚠️
- **整個結果通知沒有粗體** — `✅ 已上傳` 純文字，欄位標籤也都純文字

**問題:**
1. **P0: 上傳結果通知完全沒有粗體。** 其他所有 callback 結果都用粗體標題和標籤。上傳是唯一例外。看起來像格式壞掉。
2. `info_lines` 用 `bold=False`，不一致。
3. `reason` **沒有 escape** ⚠️（line 541, 551, 561: 直接 `{reason}`）。
4. `s3_uri` 沒有 escape 也沒有放 inline code ⚠️（line 539: `📁 目標： {s3_uri}`）。S3 URI 可能含有底線。
5. `result.get('s3_url', '')` 未 escape ⚠️。

---

### 2.16 批量上傳 callback 處理 (callbacks.py:580–685)

**觸發:** 批量上傳審批按鈕

**格式分析:**
- 進度更新：純文字 `⏳ 批量上傳中...` ✅
- 完成更新：`✅ 批量上傳完成` 純文字（**沒有粗體**）⚠️
- 使用 `build_info_lines(bold=False)` ⚠️

**問題:**
1. **P0: 批量上傳完成通知沒有粗體。** 與上傳 callback 相同問題。
2. `reason` **沒有 escape** ⚠️
3. `bold=False` 不一致。

---

### 2.17 Grant 批准後更新 (callbacks.py:56–87)

**格式分析:**
- 粗體標題 ✅
- `grant_id` 在 inline code ✅
- `user_id` 直接顯示（純數字，不需 escape）

**問題:**
1. 無嚴重問題。格式清晰。

---

### 2.18 Trust/Grant 撤銷更新 (app.py:512, 530)

**格式:**
```
🛑 *信任時段已結束*\n\n`{request_id}`
🛑 *Grant 已撤銷*\n\n`{request_id}`
```

**問題:**
1. **P2: 過於簡潔。** 沒有來源資訊或其他 context。用戶可能不記得這是哪個 trust/grant。
2. 格式一致 ✅。

---

### 2.19 已處理/已過期請求更新 (app.py:563, 593)

**格式分析:**
- 手動構造 `source_line` / `context_line`，**沒有使用 `build_info_lines()`** ⚠️
- `source` 有 escape ✅
- `context` 有 escape ✅
- `command` 有 escape **但放在 inline code 裡** ⚠️
- `reason` 有 escape ✅

**問題:**
1. **P1: `command` 先 escape 再放進 inline code。** 會導致 `\_` 在 code 裡顯示。
2. 已處理請求命令截斷 200 字元，已過期請求截斷 200 字元。一致但與審批請求的 500 不同。
3. 手動構造 source_line，不用 `build_info_lines()`。

---

### 2.20 分頁剩餘頁面 (paging.py:41)

**格式:**
```
📄 *第 {page_num}/{total_pages} 頁*\n\n```\n{content}\n```
```

**問題:**
1. **P1: 分頁內容直接放入 code block，沒有任何 escape 或截斷。** 如果 content 包含 ` ``` `，會壞掉。不過 code block 內一般不需 escape，除了 ` ``` ` 本身。
2. 分頁通知是「有聲」的（用 `send_telegram_message` 而非 `send_telegram_message_silent`）。每頁都會響鈴 ⚠️。

---

### 2.21 Telegram 命令回覆 (telegram_commands.py)

**格式:** 全部用 `parse_mode=None`（純文字）✅

**問題:**
1. **P2: 沒有格式化。** /accounts、/trust、/pending 的輸出是純文字，沒有粗體或 code，看起來比較簡陋。但這是 Telegram 命令的常見做法，不算嚴重。

---

## 3. 問題清單

### P0 — 必須修正（影響功能或 UX 嚴重不一致）

| # | 問題 | 位置 | 建議 |
|---|------|------|------|
| P0-1 | **Grant 審批請求 `source` 和 `reason` 沒有 escape** | notifications.py:207-208 | 加入 `_escape_markdown(source)` 和 `_escape_markdown(reason)` |
| P0-2 | **Grant 自動執行結果用 inline code**，多行結果會壞 | notifications.py:271 | 改用 code block ` ``` ` |
| P0-3 | **部署審批請求完全沒有粗體** | deployer.py:530-545 | 對標題和欄位標籤加 `*粗體*`，使用 `build_info_lines()` |
| P0-4 | **上傳結果通知完全沒有粗體** (bold=False) | callbacks.py:524-527 | 改用 `bold=True`（預設），或統一所有 callback 結果用粗體 |
| P0-5 | **批量上傳結果通知完全沒有粗體** (bold=False) | callbacks.py:604-607 | 同上 |
| P0-6 | **命令批准後 `reason` 沒有 escape** | callbacks.py:249, 269 | 對 `reason` 呼叫 `escape_markdown()` |

### P1 — 應該修正（不一致或潛在問題）

| # | 問題 | 位置 | 建議 |
|---|------|------|------|
| P1-1 | **Trust 自動批准 `source` 在 inline code 裡但沒 escape** | notifications.py:139 | escape 或移除 inline code（source 不應放在 code 裡） |
| P1-2 | **命令封鎖 `source` 沒有 escape** | notifications.py:351 | 加入 `_escape_markdown(source)` |
| P1-3 | **Trust 上傳 `source` 在 inline code 裡但沒 escape** | notifications.py:386 | 同 P1-1 |
| P1-4 | **Grant 自動執行結果截斷 200 字元，Trust 截斷 500** | notifications.py:271 vs 130 | 統一為 500 或引入常數 |
| P1-5 | **命令批准後顯示完整命令（未截斷）** | callbacks.py:247 | 截斷命令顯示（如 500 字元） |
| P1-6 | **上傳結果 `s3_uri` 沒放 inline code，也沒 escape** | callbacks.py:539 | 放入 inline code: `` `{s3_uri}` `` |
| P1-7 | **上傳結果 `reason` 沒有 escape** | callbacks.py:541, 551, 561 | escape |
| P1-8 | **批量上傳結果 `reason` 沒有 escape** | callbacks.py:668, 679 | escape |
| P1-9 | **部署拒絕 `reason` 沒有 escape** | callbacks.py:489 | escape（批准時有 escape，拒絕時沒有） |
| P1-10 | **`_submit_upload_for_approval` 中 `safe_s3_uri` 在 inline code 裡 double escape** | mcp_upload.py:359 | inline code 裡面不需要 escape，移除 escape |
| P1-11 | **已處理/已過期請求 `command` 在 inline code 裡 double escape** | app.py:570, 601 | 移除 escape（inline code 內不需要） |
| P1-12 | **分頁通知每頁都會響鈴** | paging.py:41 | 改用 `send_telegram_message_silent` |
| P1-13 | **build_info_lines() 被呼叫時值未 escape** | callbacks.py:214（source/context 從 DB 讀回） | 在傳入前 escape，或讓 `build_info_lines` 內部 escape |
| P1-14 | **Grant 完成通知 `reason` 沒有 escape** | notifications.py:327 | 加入 escape |
| P1-15 | **帳號 emoji 不一致：🏢 vs 🏦** | notifications.py:49 vs utils.py:45 | 統一為同一個 emoji |

### P2 — 建議改善（UX 優化）

| # | 問題 | 位置 | 建議 |
|---|------|------|------|
| P2-1 | **命令截斷長度不一致** | 各處 | 引入 `CMD_PREVIEW_SHORT = 100`、`CMD_PREVIEW_LONG = 500` 常數 |
| P2-2 | **結果截斷長度不一致** | 各處 | 引入 `RESULT_PREVIEW_SHORT = 200`、`RESULT_PREVIEW_LONG = 500` 常數 |
| P2-3 | **Grant 審批請求只顯示 account_id，沒有 account_name** | notifications.py:209 | 傳入並顯示 `{account_id} ({account_name})` |
| P2-4 | **Trust/Grant 撤銷通知過於簡潔** | app.py:512, 530 | 加入來源和簡短 context |
| P2-5 | **Grant 完成通知 `send_grant_complete_notification` 從未被呼叫** | notifications.py:320 | 在 grant 過期/完成時呼叫，或移除 dead code |
| P2-6 | **命令審批三按鈕一排在手機上可能擠** | notifications.py:75 | 考慮拆為兩排 |
| P2-7 | **Telegram 命令回覆沒有格式化** | telegram_commands.py | 可加 parse_mode=Markdown 和基本格式 |
| P2-8 | **部署審批請求沒有 `build_info_lines()`** | deployer.py:534-536 | 使用共用函數保持一致 |

---

## 4. 全域一致性建議

### 4.1 統一的通知模板結構

所有審批請求通知應遵循相同結構：

```
{emoji} *{標題}*

🤖 *來源：* {escaped_source}
📝 *任務：* {escaped_context}         ← 如有
🏦 *帳號：* `{account_id}` ({name})   ← 如有
📋 *命令/內容描述*
💬 *原因：* {escaped_reason}

🆔 *ID：* `{request_id}`
⏰ *{timeout}後過期*
```

所有結果更新通知應遵循：

```
{status_emoji} *{標題}*

🆔 *ID：* `{request_id}`
{build_info_lines(bold=True)}
📋 *命令/內容描述*
💬 *原因：* {escaped_reason}

📤 *結果：*
```{result}```
```

### 4.2 統一的 Emoji 使用規範

| 用途 | Emoji | 備註 |
|------|-------|------|
| 帳號 | 🏦 | 統一用 🏦（目前有 🏢 和 🏦 混用） |
| 來源 | 🤖 | |
| 任務/Context | 📝 | |
| 命令 | 📋 | |
| 原因 | 💬 | |
| ID | 🆔 | |
| 過期 | ⏰ | |
| 結果 | 📤 | |
| 批准 | ✅ | |
| 拒絕 | ❌ | |
| 高危 | ⚠️ | |
| 信任 | 🔓 | |
| Grant | 🔑 | |
| 部署 | 🚀 | |
| 上傳 | 📤 / 📁 | 單檔用 📤，批量用 📁 |
| 封鎖 | 🚫 | |
| 撤銷/結束 | 🛑 | |
| 進度 | 📊 | |

### 4.3 統一的 Escape 策略

**原則:**

1. **所有 user input（source、reason、context、account_name）必須 escape。** 無例外。
2. **inline code (`` ` ``) 和 code block (` ``` `) 內的文字不需要 escape。** Telegram Markdown V1 中，code entity 內的特殊字元不會被解析。
3. **因此：先決定顯示方式，再決定是否 escape。**
   - 放在 `code` 裡 → 不 escape
   - 放在普通文字裡 → 必須 escape
4. **系統值（request_id、account_id、command）通常放在 inline code 或 code block，不需 escape。**

**建議在 `build_info_lines()` 內部做 escape**，而非依賴呼叫者：

```python
def build_info_lines(source=None, context=None, ..., bold=True):
    # 內部 escape 所有 user input
    if source:
        source = _escape_markdown(source)
    if context:
        context = _escape_markdown(context)
    ...
```

這樣可以消除所有「呼叫者忘記 escape」的問題。但需注意不能 double escape（已 escape 的值不要再 escape）。一個簡單做法是**永遠在 `build_info_lines` 裡 escape，呼叫者不 escape**。

### 4.4 統一的截斷常數

建議在 `constants.py` 新增：

```python
# 通知截斷常數
CMD_PREVIEW_SHORT = 100    # 靜默通知（trust/grant auto）
CMD_PREVIEW_LONG = 500     # 審批請求
CMD_PREVIEW_RESULT = 500   # 結果更新中的命令預覽

RESULT_PREVIEW_SHORT = 200  # 靜默通知
RESULT_PREVIEW_LONG = 500   # 有聲通知
RESULT_PREVIEW_MAX = 1000   # 批准後結果

GRANT_ID_PREVIEW = 20       # Grant ID 截斷
```

### 4.5 bold=True/False 使用建議

**建議移除 `bold=False` 選項，或至少記錄何時使用。**

目前 `bold=False` 只在上傳相關 callback 中使用（callbacks.py:524, 604），導致上傳通知看起來與其他通知明顯不同。這似乎不是有意設計，而是歷史遺留。

**建議：所有通知統一用 `bold=True`。**

### 4.6 inline code vs code block 使用建議

| 內容類型 | 推薦格式 | 原因 |
|---------|---------|------|
| 命令（單行） | inline code `` `cmd` `` | 簡潔 |
| 命令（長/可能多行） | code block ` ```cmd``` ` | 防止換行壞格式 |
| 執行結果 | code block ` ```result``` ` | 結果通常多行 |
| ID/Hash | inline code | 短且不含特殊字元 |
| S3 URI | inline code | 可能含底線但 code 內不受影響 |

### 4.7 按鈕設計建議

| 通知類型 | 目前按鈕 | 建議 |
|---------|---------|------|
| 命令審批（普通） | `✅ 批准` `🔓 信任10分鐘` `❌ 拒絕` (一排) | 拆為兩排：[✅ 批准][🔓 信任10分鐘] + [❌ 拒絕] |
| 命令審批（高危） | `⚠️ 確認執行` `❌ 拒絕` (一排) | OK |
| Grant 審批 | 動態按鈕 | OK |
| 上傳 | `✅ 批准` `❌ 拒絕` (一排) | OK |
| 批量上傳 | `📁 批准上傳` `❌ 拒絕` + `🔓 批准 + 信任10分鐘` (兩排) | OK |
| 部署 | `✅ 批准部署` `❌ 拒絕` (一排) | OK |

---

## 附錄：通知代碼位置索引

| 函數 | 檔案:行 |
|------|--------|
| `send_approval_request` | notifications.py:24 |
| `send_account_approval_request` | notifications.py:85 |
| `send_trust_auto_approve_notification` | notifications.py:117 |
| `send_grant_request_notification` | notifications.py:156 |
| `send_grant_execute_notification` | notifications.py:246 |
| `send_grant_complete_notification` | notifications.py:320 |
| `send_blocked_notification` | notifications.py:337 |
| `send_trust_upload_notification` | notifications.py:363 |
| `send_batch_upload_notification` | notifications.py:400 |
| `_submit_upload_for_approval` | mcp_upload.py:310 |
| `send_deploy_approval_request` | deployer.py:504 |
| `handle_command_callback` | callbacks.py:197 |
| `handle_account_add_callback` | callbacks.py:220 |
| `handle_account_remove_callback` | callbacks.py:270 |
| `handle_deploy_callback` | callbacks.py:438 |
| `handle_upload_callback` | callbacks.py:510 |
| `handle_upload_batch_callback` | callbacks.py:580 |
| `handle_grant_approve` | callbacks.py:56 |
| `handle_grant_deny` | callbacks.py:96 |
| `_send_status_update` | callbacks.py:174 |
| 已處理請求更新 | app.py:563 |
| 已過期請求更新 | app.py:593 |
| Trust 撤銷更新 | app.py:512 |
| Grant 撤銷更新 | app.py:530 |
| 分頁通知 | paging.py:41 |
| `/accounts` 回覆 | telegram_commands.py:79 |
| `/trust` 回覆 | telegram_commands.py:99 |
| `/pending` 回覆 | telegram_commands.py:118 |
| `/help` 回覆 | telegram_commands.py:131 |
