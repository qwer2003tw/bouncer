# Sprint 13-004: Plan — GitHub PAT Expiry Monitoring

> Generated: 2026-03-05

---

## Technical Context

### PAT 使用鏈

```
deployer.py start_deploy()
  → sfn_input['github_pat_secret'] = 'sam-deployer/github-pat'
  → Step Functions → CodeBuild
  → CodeBuild: secrets-manager → GITHUB_PAT env var
  → git clone https://${GITHUB_PAT}@github.com/${GIT_REPO}.git
```

**PAT 讀取時機**：目前只在 CodeBuild 內讀取，Lambda (deployer.py) 不直接讀 PAT。
**問題**：deployer.py 需要新增 Secrets Manager 讀取權限來實現 pre-deploy 檢查。

### Secrets Manager 權限

deployer.py 的 Lambda 執行角色已有 Secrets Manager 權限：

```yaml
# deployer/template.yaml:223-224
- secretsmanager:GetSecretValue
- secretsmanager:DescribeSecret
```

Resource 限制在 `sam-deployer/*`。✅ 已有權限，不需改 template.yaml。

### 但主 Lambda (Bouncer) 可能不在 deployer stack

`deployer.py` 是在主 Bouncer Lambda 中執行的（`src/deployer.py`），不是 deployer stack 的 Lambda。
需確認主 Bouncer Lambda 的 IAM role 是否有 `secretsmanager:GetSecretValue` for `sam-deployer/github-pat`。

**如果沒有**：需在主 `template.yaml` 加權限，或改用其他方式（如在 CodeBuild 開頭檢查）。

### Design

#### `pat_checker.py`（新模組）

```python
"""GitHub PAT expiry checker."""
import logging
import urllib.request
import json
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PAT_WARN_DAYS = 7  # 提前 N 天警告

def check_github_pat(pat: str) -> dict:
    """Check PAT validity via GitHub /user API.

    Returns dict with keys:
        valid: bool
        expires_at: str or None (ISO 8601)
        days_remaining: int or None
        login: str (GitHub username)
    """
    try:
        req = urllib.request.Request(
            'https://api.github.com/user',
            headers={
                'Authorization': f'token {pat}',
                'Accept': 'application/vnd.github+json',
                'User-Agent': 'Bouncer-PAT-Checker/1.0',
            }
        )
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read())

        expiry_header = resp.headers.get('github-authentication-token-expiration')
        expires_at = None
        days_remaining = None

        if expiry_header:
            # Format: "2026-04-01 00:00:00 UTC"
            expiry_dt = datetime.strptime(expiry_header, '%Y-%m-%d %H:%M:%S %Z')
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
            expires_at = expiry_dt.isoformat()
            days_remaining = (expiry_dt - datetime.now(timezone.utc)).days

        return {
            'valid': True,
            'expires_at': expires_at,
            'days_remaining': days_remaining,
            'login': body.get('login', ''),
        }

    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {'valid': False, 'expires_at': None, 'days_remaining': None, 'login': ''}
        raise
    except Exception as e:
        logger.warning(f"PAT check failed (non-fatal): {e}")
        return None  # graceful degradation
```

#### `deployer.py` 整合

```python
def start_deploy(project_id, branch, reason, source, ...):
    # --- PAT expiry check (non-blocking for API failure) ---
    try:
        pat = _get_pat_from_secrets_manager()
        pat_info = check_github_pat(pat)

        if pat_info is None:
            # GitHub API 不可達，跳過檢查
            logger.warning("PAT expiry check skipped (API unreachable)")
        elif not pat_info['valid']:
            # PAT 已失效
            send_pat_expiry_notification(expired=True)
            return {'error': 'GitHub PAT has expired. Please renew.'}
        elif pat_info['days_remaining'] is not None and pat_info['days_remaining'] <= PAT_WARN_DAYS:
            # 即將過期
            send_pat_expiry_notification(
                days_remaining=pat_info['days_remaining'],
                expires_at=pat_info['expires_at']
            )
            # 不阻止 deploy，只警告
    except Exception as e:
        logger.warning(f"PAT expiry check error (non-fatal): {e}")

    # --- 正常 deploy 流程 ---
    ...
```

#### Secrets Manager 讀取

```python
import boto3

def _get_pat_from_secrets_manager() -> str:
    """Read GitHub PAT from Secrets Manager."""
    client = boto3.client('secretsmanager')
    response = client.get_secret_value(SecretId='sam-deployer/github-pat')
    return response['SecretString']
```

**注意**：主 Bouncer Lambda 需要 `secretsmanager:GetSecretValue` 權限（Resource: `arn:aws:secretsmanager:*:*:secret:sam-deployer/*`）。

## Risk Analysis

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| 主 Lambda 沒有 Secrets Manager 權限 | 中 | 中 | 需確認，可能需改 template.yaml |
| GitHub API rate limit | 低 | 低 | 每次 deploy 才檢查一次（低頻） |
| PAT 是 classic PAT（無過期）| 可能 | 無 | 回傳 `days_remaining=None`，跳過警告 |
| GitHub API timeout 拖慢 deploy | 低 | 低 | 10 秒 timeout + graceful degradation |
| PAT 值包含特殊字元 | 低 | 低 | PAT 是 alphanumeric，不影響 |

## Testing Strategy

- 單元測試：`check_github_pat()` — mock urllib，測 200 OK with/without expiry header
- 單元測試：401 → valid=False
- 單元測試：timeout → return None (graceful)
- 單元測試：deployer `start_deploy()` — PAT expired → 回傳 error，不啟動 SFN
- 單元測試：deployer `start_deploy()` — PAT expiring → 發通知，仍啟動 SFN
- 單元測試：deployer `start_deploy()` — PAT OK → 不發通知，正常 deploy
- 單元測試：deployer `start_deploy()` — API failure → 跳過，正常 deploy
