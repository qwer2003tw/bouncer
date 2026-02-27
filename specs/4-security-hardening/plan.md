# Sprint 4: Technical Plan

## sec-006: Credential Isolation

**Root cause:** `execute_command()` 在 src/commands.py L382-430 使用 `os.environ` 設定 STS assume role 取得的臨時 credentials。Lambda 進程內 `os.environ` 是 process-level shared state，若有任何 concurrent execution path（async handler、future SnapStart warm pool），credentials 會互相覆蓋。

**Code path:**
```python
# L388-397: 寫入 os.environ
original_env = {
    'AWS_ACCESS_KEY_ID': os.environ.get('AWS_ACCESS_KEY_ID'),
    'AWS_SECRET_ACCESS_KEY': os.environ.get('AWS_SECRET_ACCESS_KEY'),
    'AWS_SESSION_TOKEN': os.environ.get('AWS_SESSION_TOKEN'),
}
os.environ['AWS_ACCESS_KEY_ID'] = creds['AccessKeyId']
os.environ['AWS_SECRET_ACCESS_KEY'] = creds['SecretAccessKey']
os.environ['AWS_SESSION_TOKEN'] = creds['SessionToken']

# L423-430: finally block 還原
if assume_role_arn and original_env:
    for key, value in original_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
```

**Fix approach:**

### 選項 A（推薦）：subprocess 隔離 env var

使用 `subprocess.run()` 執行 `aws` CLI，透過 `env` 參數傳入隔離的環境變數。每個 request 的 credentials 完全隔離在子進程中，不動 `os.environ`。

```python
import subprocess

env = os.environ.copy()
if assume_role_arn:
    env['AWS_ACCESS_KEY_ID'] = creds['AccessKeyId']
    env['AWS_SECRET_ACCESS_KEY'] = creds['SecretAccessKey']
    env['AWS_SESSION_TOKEN'] = creds['SessionToken']
env['AWS_PAGER'] = ''

result = subprocess.run(
    args,  # ['aws', 's3', 'ls', ...]
    capture_output=True,
    text=True,
    env=env,
    timeout=55,  # Lambda timeout 60s, 留 5s buffer
)
```

- ✅ 最安全，完全隔離 env var，無 race condition
- ✅ 簡單直覺，不需理解 awscli internal
- ⚠️ 代價：fork 開銷（~50-100ms），Lambda 有 `/tmp` 空間限制
- ⚠️ 需確認 Lambda 環境中 `aws` CLI binary 可用（目前用 `awscli` Python package + `create_clidriver()`）

**注意：** 目前的實作是透過 `awscli.clidriver.create_clidriver()` 在 in-process 執行 AWS CLI（不是 subprocess）。改用 subprocess 需要確保 Lambda layer 或 package 中有 `aws` CLI binary。如果沒有，選項 B 更實際。

### 選項 B（備選，若無 CLI binary）：boto3 Session + awscli clidriver session override

保持 in-process 執行 awscli，但不修改 `os.environ`。改為建立隔離的 `botocore.session.Session` 並注入到 `create_clidriver()`。

```python
import botocore.session

session = botocore.session.Session()
if assume_role_arn:
    session.set_credentials(
        access_key=creds['AccessKeyId'],
        secret_key=creds['SecretAccessKey'],
        token=creds['SessionToken'],
    )

driver = create_clidriver(session=session)  # 需確認 create_clidriver 是否支援
```

- ✅ 無 fork 開銷
- ✅ Thread-safe（每個 request 建自己的 session）
- ⚠️ 需確認 `create_clidriver()` API 是否接受外部 session（awscli 版本依賴）
- ⚠️ 若不支援，可能需要 monkey-patch 或用其他方式注入

### 選項 C（最保守）：threading.Lock 互斥

用 `threading.Lock` 確保同一時間只有一個 request 修改 `os.environ`。

```python
_env_lock = threading.Lock()

with _env_lock:
    os.environ['AWS_ACCESS_KEY_ID'] = creds['AccessKeyId']
    ...
    try:
        exit_code = driver.main(cli_args)
    finally:
        # 還原
```

- ✅ 最小改動
- ❌ 降低並發效能（serialized execution）
- ❌ 仍然修改 global state，只是序列化了

**建議：選項 A（subprocess）最安全。若 Lambda 環境中無 `aws` binary，退而求其次用選項 B（botocore session）。選項 C 僅作為緊急 hotfix。**

**待確認：** Lambda package 中是否有 `aws` CLI binary？用 `which aws` 或 `ls /opt/` 測試。

**Files to modify:**
- `src/commands.py` — `execute_command()` L354-440
- `tests/test_commands.py` 或 `tests/test_bouncer.py` — 補 concurrent execution test

**Testing strategy:**
- 用 `threading` spawn 兩個 concurrent `execute_command()` calls，各帶不同 assume_role_arn
- Mock STS + awscli，驗證各 thread 拿到的 credentials 互不干擾
- 驗證 default account path 不受影響

---

## sec-007: Presigned URL Visibility

**Root cause:** `mcp_tool_request_presigned()` (L315) 和 `mcp_tool_request_presigned_batch()` (L574) 在成功生成 presigned URL 後不發任何通知。Admin 完全無法知道何時有 URL 被生成。

**Current flow:**
1. Parse & validate → 2. Rate limit check → 3. Resolve target → 4. Generate URL + audit record → **return（無通知）**

**Fix:** 在 Phase 4 成功後、return 前加入 Telegram 通知（silent mode）。

### 單檔通知

在 `_generate_presigned_url()` 成功路徑末尾加入：

```python
from notifications import send_silent_notification  # 或直接用 telegram module

# Fire-and-forget notification
try:
    send_telegram_message_silent(
        f"📎 Presigned URL 已生成\n"
        f"source: {ctx.source}\n"
        f"file: {ctx.filename}\n"
        f"expires: {ctx.expires_in}s\n"
        f"account: {ctx.account_id}"
    )
except Exception:
    pass  # 通知失敗不影響 URL 回傳
```

### 批次通知

在 `_generate_presigned_batch_urls()` 成功路徑末尾加入：

```python
try:
    send_telegram_message_silent(
        f"📎 Presigned URL Batch 已生成\n"
        f"source: {ctx.source}\n"
        f"files: {len(ctx.files)} 個\n"
        f"expires: {ctx.expires_in}s\n"
        f"account: {ctx.account_id}"
    )
except Exception:
    pass
```

### 重要安全原則

- **通知中絕不包含 presigned URL 本身** — URL 含有簽名，洩漏等於洩漏存取權限
- 通知 format 只含 metadata：source, filename, expiry, account

### Rate limit 分析

Presigned 已有 rate limit（`check_rate_limit(ctx.source)`，預設 5 req/60s per source）。通知頻率不會超過此限制，無需額外 rate limit 通知本身。

**Files to modify:**
- `src/mcp_presigned.py` — `_generate_presigned_url()` 和 `_generate_presigned_batch_urls()` 末尾
- `src/notifications.py` — 可新增 `send_presigned_notification()` helper（可選，直接在 mcp_presigned.py 呼叫 telegram module 也可）

**Testing strategy:**
- Mock `send_telegram_message_silent`，驗證成功 path 呼叫了通知
- 驗證失敗 path（rate limit, validation error）不呼叫通知
- 驗證通知內容不含 presigned URL

---

## sec-008: Grant Pattern ReDoS

**Root cause:** `compile_pattern()` (src/grant.py L84-122) 將 user-provided pattern 中的 `*` 轉為 `\S*`、`**` 轉為 `.*`。如果 pattern 含有大量連續 wildcard（如 `*****`），生成的 regex 會是 `\S*\S*\S*\S*\S*`，在不匹配時產生 catastrophic backtracking。

**Attack vector:** 攻擊者（或無心的使用者）透過 `bouncer_request_grant` 提交惡意 pattern，在後續 `match_pattern()` 時觸發 ReDoS，導致 Lambda timeout。

**具體分析：**

`_glob_to_regex()` (L125-148) 的處理邏輯：
```python
escaped = re.escape(text)
escaped = escaped.replace(r'\*\*', '.*')    # ** → .*
escaped = escaped.replace(r'\*', r'\S*')    # * → \S*
```

問題案例：
- Pattern `*****` → regex `\S*\S*\S*\S*\S*` → 5 個 `\S*` 串聯
- 對不匹配的長字串（如 1000 個 `a`），regex engine 需嘗試所有分割組合 → O(n^k)

**Fix approach:**

### 1. 前置驗證（在 `compile_pattern()` 開頭）

```python
def compile_pattern(pattern: str) -> re.Pattern:
    # === 前置驗證 ===
    if len(pattern) > 200:
        raise ValueError("Pattern 長度超過上限（200 字元）")

    # 計算 wildcard 數量（排除 placeholder 內的 *）
    # 先移除所有 {name} placeholder，再數 *
    stripped = re.sub(r'\{[a-zA-Z_][a-zA-Z0-9_]*\}', '', pattern)
    star_count = stripped.count('*')
    if star_count > 10:  # ** 算 2 個 *，所以 5 個 ** = 10 個 *
        raise ValueError("Pattern 含有過多 wildcard（上限 5 個 **）")

    # 禁止連續 ** 之後又 ** (e.g., ****)
    if re.search(r'\*{3,}', pattern):
        raise ValueError("Pattern 含有不合法的連續 wildcard")

    # ... 現有邏輯 ...
```

### 2. regex compile 異常 catch（在末尾）

```python
    try:
        return re.compile(f'^{full_regex}$', re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"Pattern 編譯失敗: {e}")
```

### 3. match_pattern() timeout 防護（可選加強）

```python
import signal

def match_pattern(pattern: str, normalized_cmd: str) -> bool:
    compiled = compile_pattern(pattern)
    # 可選：用 re2 或 timeout 限制
    return bool(compiled.match(normalized_cmd))
```

**Files to modify:**
- `src/grant.py` — `compile_pattern()` 加前置驗證 + `re.error` catch

**Testing strategy:**
- 正常 pattern → 正常 compile + match
- Pattern > 200 chars → ValueError
- Pattern 含 6+ wildcard → ValueError
- Pattern 含 `****` → ValueError
- 合法但複雜的 pattern（5 wildcards）→ match 在 100ms 內完成
- `re.error` 路徑 → ValueError with message

---

## ops-001: Duration Alarm 閾值修正

**Root cause:** `template.yaml` L453 `Threshold: 600000` (600 秒)，但 Lambda timeout 是 60 秒 (L43)。600 秒 alarm 永遠不會觸發。

**Fix:** 改為 `Threshold: 50000` (50 秒)，在 Lambda timeout (60s) 的 ~83% 觸發告警。

```yaml
# Before
Threshold: 600000

# After
Threshold: 50000
```

**Rationale:** 50 秒 = 60 秒 timeout 的 83%。用 p99 統計量，如果 p99 > 50s 表示有些 invocation 接近 timeout，需要調查。留 10s buffer 讓告警有時間送出。

**Files to modify:**
- `template.yaml` L453

---

## ops-003: SNS Subscription

**Root cause:** `AlarmNotificationTopic` (template.yaml L413-415) 有建立 SNS Topic，所有 CloudWatch Alarms 都發到此 topic，但沒有任何 Subscription。告警等於送進黑洞。

**Fix:** 加入 `AWS::SNS::Subscription` resource，email 來源用 CloudFormation Parameter + Condition。

### 方案 A（推薦）：CloudFormation Parameter

```yaml
Parameters:
  # ... 既有 parameters ...
  AlarmEmail:
    Type: String
    Default: ""
    Description: "Email address for alarm notifications (leave empty to skip subscription)"

Conditions:
  HasAlarmEmail: !Not [!Equals [!Ref AlarmEmail, ""]]

Resources:
  # ... 既有 resources ...
  AlarmEmailSubscription:
    Type: AWS::SNS::Subscription
    Condition: HasAlarmEmail
    Properties:
      TopicArn: !Ref AlarmNotificationTopic
      Protocol: email
      Endpoint: !Ref AlarmEmail
```

- ✅ 部署時可選填，空值 = 不建 subscription
- ✅ 標準 CloudFormation pattern
- ✅ 無需依賴 SSM Parameter Store

### 方案 B（備選）：SSM Parameter Store

```yaml
Parameters:
  AlarmEmailParam:
    Type: AWS::SSM::Parameter::Value<String>
    Default: /bouncer/alarm-email
```

- ✅ Email 可以在不重新部署的情況下更改（但 subscription 仍需 stack update）
- ❌ 多一個 SSM parameter 要管理
- ❌ SSM parameter 必須事先存在，否則 deploy 失敗

**建議：方案 A**（簡單、無外部依賴）

**Files to modify:**
- `template.yaml` — 加 `AlarmEmail` parameter, `HasAlarmEmail` condition, `AlarmEmailSubscription` resource

**Testing strategy:**
- 部署時帶 `AlarmEmail=xxx@example.com` → subscription 建立
- 部署時不帶或空值 → subscription 不建立、stack 正常
- 確認既有 alarms 的 `AlarmActions` 仍指向同一 topic

---

## 變更影響分析

| 變更 | 影響範圍 | 風險等級 | Rollback |
|------|----------|----------|----------|
| sec-006 | commands.py | 中 — 改 credential 傳遞機制 | Revert commit |
| sec-007 | mcp_presigned.py | 低 — 只加通知，fire-and-forget | Revert commit |
| sec-008 | grant.py | 低 — 只加前置驗證 | Revert commit |
| ops-001 | template.yaml | 低 — 改一個數字 | 改回 600000 |
| ops-003 | template.yaml | 低 — 加 conditional resource | 刪除 resource |

**部署順序建議：**
1. sec-008 (ReDoS) — 最小風險，先部署驗證流程
2. sec-007 (presigned notification) — 低風險
3. ops-001 + ops-003 (template fix) — 一起部署
4. sec-006 (credential isolation) — 最大變更，最後部署，充分測試
