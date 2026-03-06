# Sprint 6-003: 過期請求按鈕自動移除（EventBridge Scheduler）

## Summary

使用 EventBridge Scheduler 在請求建立時同步排程 one-shot 清除任務，TTL 到期後自動移除 Telegram 按鈕並更新 DynamoDB 狀態為 `timeout`。

## Root Cause / Background

### 問題

目前 Bouncer 的過期處理是 **被動式**——只有當用戶點擊已過期請求的按鈕時（`app.py` 行 ~268-290），才會：
1. `answer_callback` 回覆「⏰ 此請求已過期」
2. DynamoDB 更新 `status=timeout`
3. `update_message` 移除按鈕

如果沒有人點擊，按鈕會永遠留在 Telegram 聊天中，造成：
- 視覺混亂（看不出哪些請求還 pending、哪些已過期）
- 安全風險（過期後很久才點擊，用戶可能忘記請求內容）
- DynamoDB 的 `status` 仍為 `pending_approval`，直到 DynamoDB TTL 自動刪除

### 現有過期處理（被動）

**`src/app.py` `handle_telegram_webhook`**（行 ~268-290）：
```python
# 檢查是否過期
ttl = item.get('ttl', 0)
if ttl and int(time.time()) > ttl:
    answer_callback(callback['id'], '⏰ 此請求已過期')
    table.update_item(
        Key={'request_id': request_id},
        UpdateExpression='SET #s = :s',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': 'timeout'}
    )
    # 更新 Telegram 訊息，移除按鈕
    if message_id:
        update_message(message_id, expired_text, remove_buttons=True)
    return response(200, {'ok': True, 'expired': True})
```

### 現有 TTL 值

| 請求類型 | TTL | 來源 |
|----------|-----|------|
| execute | `now + MCP_MAX_WAIT(30s) + APPROVAL_TTL_BUFFER(60s)` = 90s | `mcp_execute.py` `_submit_for_approval` |
| upload / upload_batch | `now + UPLOAD_TIMEOUT(300s) + APPROVAL_TTL_BUFFER(60s)` = 360s | `mcp_upload.py` |
| deploy | `now + APPROVAL_TIMEOUT_DEFAULT(300s) + APPROVAL_TTL_BUFFER(60s)` = 360s | `deployer.py` (推測) |
| add_account / remove_account | `now + 300 + APPROVAL_TTL_BUFFER(60s)` = 360s | 各 handler |
| grant (審批階段) | `now + GRANT_APPROVAL_TIMEOUT(300s)` = 300s | `grant.py` `create_grant_request` |

### Telegram Message ID 需求

要移除按鈕，需要知道 Telegram `message_id`。目前 DynamoDB 中沒有儲存 `message_id`。

- `send_approval_request`（`notifications.py`）回傳 Telegram API response，但沒有把 `message_id` 存回 DynamoDB。
- `send_batch_upload_notification`（`notifications.py`）同上。
- 需要在請求建立後，把 Telegram `message_id` 存入 DynamoDB。

## Acceptance Criteria

### AC-1: 請求建立時排程清除
- **Given** 任何需要人工審批的請求被建立（execute / upload / upload_batch / deploy / add_account / remove_account / grant）
- **When** 請求成功寫入 DynamoDB 且 Telegram 通知發送成功
- **Then** 同時建立一個 EventBridge Scheduler one-shot schedule，觸發時間 = 請求的 TTL 到期時

### AC-2: TTL 到期自動清除
- **Given** Scheduler 在 TTL 到期時觸發
- **When** 目標 Lambda 被呼叫
- **Then**
  1. 讀取 DynamoDB item
  2. 如果 `status` 仍為 `pending` 或 `pending_approval`，更新為 `timeout`
  3. 使用儲存的 `message_id` 呼叫 Telegram API `editMessageText`（移除按鈕，顯示過期訊息）
  4. 刪除 Scheduler schedule（one-shot 清除）

### AC-3: 已處理的請求不受影響
- **Given** 請求在 TTL 前已被 approve/deny
- **When** Scheduler 觸發
- **Then** 讀取 DynamoDB 發現 `status != pending/pending_approval`，跳過處理，只刪除 schedule

### AC-4: message_id 儲存
- **Given** 請求建立時 Telegram 通知發送成功
- **When** 收到 Telegram API response
- **Then** `message_id` 被存入 DynamoDB item

### AC-5: Scheduler 建立失敗不影響主流程
- **Given** EventBridge Scheduler 建立失敗（API 錯誤、權限不足等）
- **When** 請求建立流程
- **Then** 請求仍正常建立，只是沒有自動清除（退化為被動清除）
- **And** 錯誤被 log 但不回傳給 client

## Implementation Plan

### 架構概覽

```
請求建立
  ├── DynamoDB put_item
  ├── Telegram send_message → 取得 message_id
  ├── DynamoDB update_item (存 message_id)
  └── EventBridge Scheduler create_schedule (one-shot, at TTL time)
            │
            ▼ (TTL 到期時)
       Lambda (cleanup endpoint)
  ├── DynamoDB get_item → 確認 status == pending
  ├── DynamoDB update_item → status = timeout
  ├── Telegram editMessageText → 移除按鈕
  └── Scheduler delete_schedule → 清除自身
```

### 1. 新增 Lambda endpoint：cleanup handler

#### 方案選擇

**方案 A（推薦）：共用現有 Lambda + 新增 API path**
- 在現有 `ApprovalFunction` 的路由中新增 `/cleanup` path
- EventBridge Scheduler 透過 API Gateway 呼叫
- 優點：不需要新 Lambda，冷啟動少
- 缺點：cleanup 請求走 API Gateway（但這是內部觸發，無安全問題）

**方案 B：獨立 Lambda**
- 新建一個 cleanup-only Lambda
- EventBridge Scheduler 直接 invoke
- 優點：職責分離
- 缺點：多一個 Lambda cold start + IAM 配置

**選擇方案 A**：cleanup 邏輯輕量（DynamoDB read + update + Telegram API call），不需要獨立 Lambda。

#### 檔案：`src/app.py`

新增 cleanup path routing：

```python
# 在 lambda_handler 路由中新增
elif path.endswith('/cleanup'):
    return handle_cleanup_request(event)
```

新增 handler：

```python
def handle_cleanup_request(event: dict) -> dict:
    """處理 EventBridge Scheduler 觸發的過期清除"""
    try:
        body = json.loads(event.get('body', '{}'))
    except:
        return response(400, {'error': 'Invalid JSON'})

    request_id = body.get('request_id', '')
    schedule_name = body.get('schedule_name', '')

    if not request_id:
        return response(400, {'error': 'Missing request_id'})

    # 讀取 DynamoDB
    item = table.get_item(Key={'request_id': request_id}).get('Item')
    if not item:
        _delete_schedule(schedule_name)
        return response(200, {'ok': True, 'action': 'not_found'})

    status = item.get('status', '')

    # 只處理 pending 狀態
    if status not in ('pending', 'pending_approval'):
        _delete_schedule(schedule_name)
        return response(200, {'ok': True, 'action': 'already_processed', 'status': status})

    # 更新 DynamoDB
    table.update_item(
        Key={'request_id': request_id},
        UpdateExpression='SET #s = :s, timed_out_at = :t',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': 'timeout', ':t': int(time.time())}
    )

    # 移除 Telegram 按鈕
    message_id = item.get('telegram_message_id')
    if message_id:
        _cleanup_telegram_message(message_id, item)

    # 清除 schedule
    _delete_schedule(schedule_name)

    return response(200, {'ok': True, 'action': 'cleaned_up'})
```

### 2. 儲存 Telegram message_id

#### 問題

目前 `send_approval_request`、`send_batch_upload_notification` 等函數呼叫 `send_telegram_message`，但回傳值沒有被用來存 `message_id`。

#### 修改方案

需要修改所有發送審批通知的地方，在 Telegram 回傳後把 `message_id` 存入 DynamoDB。

#### 檔案：`src/notifications.py`

`send_telegram_message` 已回傳 Telegram API response dict（含 `result.message_id`）。修改各 notification 函數，回傳 `message_id`：

```python
def send_approval_request(...) -> bool:  # 改為回傳 (bool, int|None)
    result = _send_message(text, keyboard)
    ok = bool(result and result.get('ok'))
    msg_id = result.get('result', {}).get('message_id') if ok else None
    return ok, msg_id  # BREAKING: 需要更新所有呼叫端
```

**但這是 breaking change**，影響多處。

**替代方案（推薦）：在呼叫端存 message_id**

在 `mcp_execute.py` `_submit_for_approval`、`mcp_upload.py` 等呼叫端：

```python
# 現有
notified = send_approval_request(...)

# 改為
tg_result = _telegram.send_telegram_message(message, keyboard)  # 直接呼叫
if tg_result and tg_result.get('ok'):
    msg_id = tg_result.get('result', {}).get('message_id')
    if msg_id:
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET telegram_message_id = :mid',
            ExpressionAttributeValues={':mid': msg_id}
        )
```

**更好的方案：notification 函數回傳 message_id**

修改 `send_approval_request` 回傳型別為 `Optional[int]`（回傳 message_id 或 None），而非 `bool`。呼叫端判斷 truthy 即可向後相容。

```python
def send_approval_request(...) -> Optional[int]:
    """回傳 Telegram message_id (成功) 或 None (失敗)。
    
    Note: 回傳值是 truthy/falsy，與舊的 bool 回傳相容。
    """
    result = _send_message(text, keyboard)
    if result and result.get('ok'):
        return result.get('result', {}).get('message_id')
    return None
```

同理修改 `send_batch_upload_notification`、`send_grant_request_notification`、`send_account_approval_request`。

呼叫端：
```python
msg_id = send_approval_request(...)
if not msg_id:
    raise RuntimeError("Telegram notification failed")
# 存 message_id
table.update_item(
    Key={'request_id': request_id},
    UpdateExpression='SET telegram_message_id = :mid',
    ExpressionAttributeValues={':mid': msg_id}
)
# 排程 cleanup
_schedule_cleanup(request_id, ttl)
```

### 3. 排程 EventBridge Scheduler

#### 新增工具函數

#### 檔案：`src/scheduler.py`（新增）

```python
"""EventBridge Scheduler helper for request cleanup."""
import boto3
import json
import time
from datetime import datetime, timezone

# Lazy init
_scheduler_client = None

def _get_client():
    global _scheduler_client
    if _scheduler_client is None:
        _scheduler_client = boto3.client('scheduler')
    return _scheduler_client

def schedule_cleanup(
    request_id: str,
    ttl_epoch: int,
    api_url: str,
    schedule_role_arn: str,
) -> str | None:
    """建立 one-shot EventBridge Schedule 在 TTL 到期時清除請求。

    Args:
        request_id: DynamoDB request_id
        ttl_epoch: Unix timestamp (TTL 到期時間)
        api_url: Cleanup endpoint URL (API Gateway)
        schedule_role_arn: Scheduler assume 的 IAM Role ARN

    Returns:
        Schedule name (成功) 或 None (失敗)
    """
    try:
        client = _get_client()
        schedule_name = f"bouncer-cleanup-{request_id}"

        # 確保時間在未來
        now = int(time.time())
        if ttl_epoch <= now:
            ttl_epoch = now + 5  # 最少 5 秒後

        # 轉為 ISO 8601
        schedule_time = datetime.fromtimestamp(ttl_epoch, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

        client.create_schedule(
            Name=schedule_name,
            GroupName='bouncer-cleanup',
            ScheduleExpression=f'at({schedule_time})',
            ScheduleExpressionTimezone='UTC',
            FlexibleTimeWindow={'Mode': 'OFF'},
            Target={
                'Arn': api_url,  # API Gateway ARN 或 Lambda ARN
                'RoleArn': schedule_role_arn,
                'Input': json.dumps({
                    'request_id': request_id,
                    'schedule_name': schedule_name,
                }),
                # 如果用 API Gateway 作為 target：
                # 'HttpParameters': {
                #     'PathParameterValues': [],
                #     'QueryStringParameters': {},
                #     'HeaderParameters': {'Content-Type': 'application/json'},
                # },
            },
            ActionAfterCompletion='DELETE',  # one-shot: 執行後自動刪除
        )

        return schedule_name

    except Exception as e:
        print(f"[SCHEDULER] Failed to create schedule for {request_id}: {e}")
        return None

def delete_schedule(schedule_name: str) -> bool:
    """刪除 EventBridge Schedule。

    注意：如果 ActionAfterCompletion=DELETE 已生效，此呼叫會得到
    ResourceNotFoundException，視為成功。
    """
    if not schedule_name:
        return True
    try:
        client = _get_client()
        client.delete_schedule(
            Name=schedule_name,
            GroupName='bouncer-cleanup',
        )
        return True
    except client.exceptions.ResourceNotFoundException:
        return True  # 已自動刪除
    except Exception as e:
        print(f"[SCHEDULER] Failed to delete schedule {schedule_name}: {e}")
        return False
```

### 4. EventBridge Scheduler Target 設計

#### 方案 A（推薦）：Scheduler → Lambda（直接 invoke）

EventBridge Scheduler 直接 invoke Lambda 函數。Lambda event 格式：

```json
{
    "request_id": "req_abc123",
    "schedule_name": "bouncer-cleanup-req_abc123"
}
```

在 `app.py` `lambda_handler` 中偵測非 API Gateway event：

```python
def lambda_handler(event, context):
    # EventBridge Scheduler 直接 invoke（沒有 path/rawPath）
    if 'request_id' in event and 'schedule_name' in event:
        return handle_cleanup_event(event)
    
    # 正常 API Gateway routing...
    path = event.get('rawPath') or event.get('path') or '/'
    ...
```

這比走 API Gateway 更簡單，不需要新增 API path。

### 5. template.yaml 新增資源

#### 檔案：`template.yaml`

```yaml
# ============================================================
# EventBridge Scheduler - Request Cleanup
# ============================================================

# Schedule Group（所有 cleanup schedule 放在同一 group，方便管理）
CleanupScheduleGroup:
  Type: AWS::Scheduler::ScheduleGroup
  Properties:
    Name: bouncer-cleanup
    Tags:
      - Key: Project
        Value: Bouncer

# IAM Role for Scheduler to invoke Lambda
SchedulerExecutionRole:
  Type: AWS::IAM::Role
  Properties:
    RoleName: !Sub "bouncer-${Environment}-scheduler-role"
    AssumeRolePolicyDocument:
      Version: '2012-10-17'
      Statement:
        - Effect: Allow
          Principal:
            Service: scheduler.amazonaws.com
          Action: sts:AssumeRole
          Condition:
            StringEquals:
              aws:SourceAccount: !Ref AWS::AccountId
    Policies:
      - PolicyName: InvokeLambda
        PolicyDocument:
          Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action: lambda:InvokeFunction
              Resource:
                - !GetAtt ApprovalFunction.Arn
                - !Sub "${ApprovalFunction.Arn}:*"
    Tags:
      - Key: Project
        Value: Bouncer
```

#### Lambda 新增環境變數

```yaml
# ApprovalFunction Environment Variables 新增：
CLEANUP_SCHEDULE_ROLE_ARN: !GetAtt SchedulerExecutionRole.Arn
CLEANUP_LAMBDA_ARN: !GetAtt ApprovalFunction.Arn
```

#### Lambda 新增 IAM Policy

```yaml
# ApprovalFunction Policies 新增：
- Sid: EventBridgeSchedulerAccess
  Effect: Allow
  Action:
    - scheduler:CreateSchedule
    - scheduler:DeleteSchedule
    - scheduler:GetSchedule
  Resource:
    - !Sub "arn:aws:scheduler:${AWS::Region}:${AWS::AccountId}:schedule/bouncer-cleanup/*"
- Sid: PassSchedulerRole
  Effect: Allow
  Action: iam:PassRole
  Resource: !GetAtt SchedulerExecutionRole.Arn
  Condition:
    StringEquals:
      iam:PassedToService: scheduler.amazonaws.com
```

### 6. 修改請求建立流程

需要在以下地方加入 `schedule_cleanup` 呼叫：

| 入口 | 檔案 | 行號（約） | 說明 |
|------|------|-----------|------|
| `_submit_for_approval` | `src/mcp_execute.py` | ~395 | execute 請求 |
| `_submit_upload_for_approval` | `src/mcp_upload.py` | ~250 | upload 單檔 |
| `mcp_tool_upload_batch` | `src/mcp_upload.py` | ~395 | upload_batch |
| `mcp_tool_deploy` | `src/deployer.py` | TBD | deploy 請求 |
| `mcp_tool_add_account` / `mcp_tool_remove_account` | `src/mcp_admin.py` | TBD | 帳號管理 |
| `create_grant_request` + notification | `src/mcp_execute.py` `mcp_tool_request_grant` | ~440 | grant 審批 |
| REST `handle_clawdbot_request` | `src/app.py` | ~185 | REST API |

每處的修改模式相同：
```python
# 在 Telegram 通知成功後
msg_id = send_xxx_notification(...)  # 回傳 message_id
if msg_id:
    table.update_item(
        Key={'request_id': request_id},
        UpdateExpression='SET telegram_message_id = :mid',
        ExpressionAttributeValues={':mid': msg_id}
    )
    from scheduler import schedule_cleanup
    schedule_cleanup(
        request_id=request_id,
        ttl_epoch=ttl,
        api_url=os.environ.get('CLEANUP_LAMBDA_ARN', ''),
        schedule_role_arn=os.environ.get('CLEANUP_SCHEDULE_ROLE_ARN', ''),
    )
```

### 7. Scheduler TTL 與請求 TTL 對應

| 請求類型 | 請求 TTL（秒） | Scheduler 觸發時間 | 說明 |
|----------|------------|-----------------|------|
| execute | now + 90 | now + 90 | 與 DynamoDB ttl 一致 |
| upload | now + 360 | now + 360 | 與 DynamoDB ttl 一致 |
| upload_batch | now + 360 | now + 360 | 與 DynamoDB ttl 一致 |
| deploy | now + 360 | now + 360 | 與 DynamoDB ttl 一致 |
| account add/remove | now + 360 | now + 360 | 與 DynamoDB ttl 一致 |
| grant (審批) | now + 300 | now + 300 | GRANT_APPROVAL_TIMEOUT |

Scheduler 觸發時間 = DynamoDB item 的 `ttl` 值，保證一致性。

### 8. Cleanup 後的被動檢查

被動過期檢查（`app.py` 行 ~268-290）**仍保留**。原因：
1. Scheduler 可能因任何原因未觸發（Scheduler 服務暫時不可用、IAM 權限問題等）
2. 作為安全網：即使 Scheduler 失敗，用戶點擊按鈕仍會觸發過期處理
3. 已處理的 `status=timeout` 會被 `status not in ['pending_approval', 'pending']` 檢查攔截

### 9. 請求被 approve/deny 後取消 Scheduler

當請求被 approve/deny 時，應取消對應的 Scheduler（避免無用觸發）。

修改 `_update_request_status`（`src/callbacks.py` 行 ~130-165）：

```python
def _update_request_status(table, request_id, status, approver, extra_attrs=None):
    # ... 現有邏輯 ...
    
    # 取消 cleanup schedule
    try:
        from scheduler import delete_schedule
        delete_schedule(f"bouncer-cleanup-{request_id}")
    except Exception:
        pass  # Non-critical
```

## Test Plan

### 新增測試

1. **`tests/test_scheduler.py`**（新增）
   - 測試 `schedule_cleanup` 正確呼叫 `scheduler.create_schedule`
   - 測試 `delete_schedule` 正確處理 ResourceNotFoundException
   - 測試 TTL 在過去的情況（應設為 now + 5）
   - Mock `boto3.client('scheduler')`

2. **`tests/test_cleanup_handler.py`**（新增）
   - 測試 `handle_cleanup_event`：
     - status=pending → 更新為 timeout + editMessage
     - status=approved → 跳過處理
     - item 不存在 → 安全返回
   - Mock DynamoDB + Telegram API

3. **`tests/test_message_id_storage.py`**（新增）
   - 測試 notification 函數回傳 message_id
   - 測試呼叫端正確將 message_id 存入 DynamoDB

### 修改現有測試

4. **`tests/test_callbacks.py`**
   - Mock `scheduler.delete_schedule`，避免在 `_update_request_status` 中出錯

### 手動驗證

5. 部署後：
   - 發起 execute 請求，不審批，等待 TTL → 確認按鈕自動移除
   - 發起 upload_batch 請求，不審批，等待 TTL → 確認按鈕自動移除
   - 發起 execute 請求，立刻審批 → 確認 Scheduler 被取消
6. 在 AWS Console 確認：
   - `bouncer-cleanup` schedule group 存在
   - 請求建立時有新 schedule
   - TTL 後 schedule 被 `ActionAfterCompletion=DELETE` 自動刪除

### 成本評估

- EventBridge Scheduler 免費額度：每月 14,000,000 次調用
- Bouncer 日均請求量 << 1000，年成本約 $0

## Out of Scope

- 不修改 DynamoDB TTL 機制（仍保留自動刪除）
- 不修改被動過期檢查邏輯（保留作為安全網）
- 不處理 Trust Session 的過期按鈕移除（Trust 有獨立的 revoke 機制）
- 不加入 retry 機制（Scheduler 觸發失敗依靠被動檢查兜底）
- 不建立 CloudWatch Alarm for Scheduler 失敗（Phase 2 再考慮）
- 不做 schedule 的批量清理（依靠 `ActionAfterCompletion=DELETE`）
- 不修改 Grant 批准後的使用期限清除（Grant 有獨立的 DynamoDB TTL）
