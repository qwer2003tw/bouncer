# Sprint 13-004: GitHub PAT Expiry Monitoring

> GitHub Issue: #57
> Priority: P1
> TCS: 5
> Generated: 2026-03-05

---

## Problem Statement

Bouncer Deployer 使用 GitHub Personal Access Token (PAT) 來 clone private repo 進行 SAM deploy：

```yaml
# deployer/template.yaml:484-496
env:
  secrets-manager:
    GITHUB_PAT: ${GITHUB_PAT_SECRET}
commands:
  - git clone --depth 1 -b ${GIT_BRANCH} https://${GITHUB_PAT}@github.com/${GIT_REPO}.git repo
```

PAT 儲存在 Secrets Manager（`sam-deployer/github-pat`）。**GitHub fine-grained PAT 有過期日**，過期後：
1. `git clone` 失敗 → CodeBuild 失敗 → deploy 失敗
2. **沒有任何提前警告** — 只有 deploy 失敗後才發現
3. 排查成本高 — deploy 失敗原因可能被誤判為其他問題

### 目標

新增 PAT 過期日監控，在過期前 N 天發 Telegram 警告通知。

## Root Cause

目前沒有機制檢查 GitHub PAT 的有效性或過期日。PAT 是 static secret，Secrets Manager 不會自動 rotate。

## User Stories

**US-1: PAT expiry warning**
As the **admin (Steven)**,
I want to receive a Telegram warning before the GitHub PAT expires,
So that I can renew it before deploy starts failing.

**US-2: PAT expiry check on deploy**
As the **Bouncer system**,
I want to check PAT expiry before starting a deploy,
So that I can warn early instead of failing mid-deploy.

## Scope

### 方案：Deploy 前檢查 + 定期檢查

#### Part 1: Deploy 前檢查（deployer.py / Lambda 內）

在 `start_deploy()` 開始 Step Functions 之前，呼叫 GitHub API 檢查 PAT 有效性：

```python
import urllib.request
import json

def check_github_pat_expiry(pat: str) -> dict:
    """Check GitHub PAT validity and expiry via GitHub API.

    Returns:
        {
            'valid': bool,
            'expires_at': str or None,  # ISO 8601
            'days_remaining': int or None,
            'scopes': str,
        }
    """
    req = urllib.request.Request(
        'https://api.github.com/user',
        headers={
            'Authorization': f'token {pat}',
            'Accept': 'application/vnd.github+json',
        }
    )
    resp = urllib.request.urlopen(req, timeout=10)
    # GitHub 回傳 'github-authentication-token-expiration' header
    # 格式：2026-04-01 00:00:00 UTC
    expiry = resp.headers.get('github-authentication-token-expiration')
    ...
```

**GitHub API 行為**：
- `GET /user` with PAT → 200 OK + response headers
- Header `github-authentication-token-expiration` 包含 PAT 過期時間（fine-grained PAT）
- Classic PAT 不含此 header（無過期日）

#### Part 2: 警告邏輯

```python
# deployer.py — start_deploy() 開頭
pat = _get_github_pat()  # 從 Secrets Manager 讀取
pat_info = check_github_pat_expiry(pat)

if not pat_info['valid']:
    # PAT 已失效 → 阻止 deploy + 發 Telegram 警告
    send_pat_expiry_notification(expired=True)
    return error

if pat_info['days_remaining'] is not None and pat_info['days_remaining'] <= 7:
    # PAT 將在 7 天內過期 → 發 Telegram 警告（不阻止 deploy）
    send_pat_expiry_notification(
        days_remaining=pat_info['days_remaining'],
        expires_at=pat_info['expires_at']
    )
```

#### Part 3: Telegram 通知

新增 `send_pat_expiry_notification()` 到 notifications.py：

```
⚠️ GitHub PAT 即將過期

🔑 sam-deployer/github-pat
📅 過期日：2026-04-01
⏳ 剩餘：5 天

請及時更新 PAT 以避免 deploy 失敗。
```

#### Part 4: 定期檢查（可選 — EventBridge Schedule）

用 EventBridge Scheduler 每天觸發一次 Lambda 檢查 PAT 狀態。但這需要 template.yaml 變更（新 Lambda 或新 Schedule），複雜度較高。

**本 sprint 先做 Part 1-3（deploy 前檢查）**。Part 4 可放到後續 sprint。

## Out of Scope

- 不自動 rotate PAT（GitHub PAT 不支援自動 rotation）
- 不改 Secrets Manager rotation 設定
- 不改 CodeBuild 的 git clone 邏輯
- Part 4 定期檢查（EventBridge Schedule）留後續 sprint

## Acceptance Criteria

1. Deploy 前會檢查 GitHub PAT 有效性
2. PAT 過期 → deploy 被阻止 + Telegram 警告
3. PAT 7 天內過期 → Telegram 警告（不阻止 deploy）
4. Classic PAT（無過期日）→ 跳過檢查，不影響 deploy
5. GitHub API 不可達 → 跳過檢查（graceful degradation），不影響 deploy
6. 新增測試覆蓋各種情境（expired / expiring / no-expiry / API failure）
