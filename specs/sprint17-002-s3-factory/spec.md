# Sprint 17-002: S3 Client Factory — 消除 6 處重複 STS/S3 模式

> GitHub Issue: #79
> Priority: P1
> TCS: 6
> Generated: 2026-03-08

---

## Problem Statement

完全相同的 STS assume role → S3 client 建立模式在 6 處重複：
- `mcp_upload.py`：4 處（line ~315, ~401, ~808, ~1020）
- `callbacks.py`：2 處（line ~704, ~932）

每處約 15-20 行，總共 ~100 行重複程式碼。Bug 修一處漏五處，mock 測試要重複 6 次。

## Root Cause

S3 client 建立（含可選的 cross-account STS assume role）是最初各功能獨立開發時各自 inline 的，沒有統一入口。

## Scope

### 變更 1: 新增 `get_s3_client()` 工廠函數

**檔案：** `src/aws_clients.py`（新增）

```python
"""Centralized AWS client factory functions."""
import boto3
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_boto3 = boto3  # allow test monkeypatching


def get_s3_client(
    role_arn: Optional[str] = None,
    account_id: Optional[str] = None,
    region: Optional[str] = None,
):
    """Build boto3 S3 client, optionally assuming a cross-account role.
    
    Args:
        role_arn: If provided, assume this role before creating S3 client.
        account_id: Used in RoleSessionName for audit trail.
        region: AWS region for S3 client.
    
    Returns:
        boto3 S3 client
    """
    if role_arn:
        sts = _boto3.client('sts')
        session_name = f'bouncer-{account_id or "default"}'[:64]
        creds = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
        )['Credentials']
        return _boto3.client(
            's3',
            aws_access_key_id=creds['AccessKeyId'],
            aws_secret_access_key=creds['SecretAccessKey'],
            aws_session_token=creds['SessionToken'],
            region_name=region,
        )
    return _boto3.client('s3', region_name=region)
```

### 變更 2: 替換 mcp_upload.py 中 4 處

**檔案：** `src/mcp_upload.py`

替換各處 inline STS + S3 client 為：
```python
from aws_clients import get_s3_client

s3 = get_s3_client(
    role_arn=ctx.assume_role,  # 或 role_arn 變數
    account_id=ctx.account_id,  # 或 account_id 變數
    region=region,
)
```

**4 處位置：**
1. line ~314-327：`_do_upload_and_verify()` 內
2. line ~398-410：presigned 相關 S3 操作
3. line ~808：batch upload 的 S3 client
4. line ~1020：deploy_frontend 的 S3 client

### 變更 3: 替換 callbacks.py 中 2 處

**檔案：** `src/callbacks.py`

替換 inline STS + S3 client 為 `get_s3_client()` 呼叫。

**2 處位置：**
1. line ~704：upload confirm callback
2. line ~932：batch upload confirm callback

### 保持不變

- `mcp_presigned.py` 中的 `boto3.client("s3")` — 這些不涉及 assume role，使用預設 credential，不需要工廠函數。如有需要可在後續統一。
- `mcp_confirm.py` 中的 `boto3.client("s3")` — 同上理由。

## 設計決策

| 決策 | 選項 | 選擇 | 理由 |
|------|------|------|------|
| 放置位置 | utils.py vs 新檔案 | `aws_clients.py`（新檔案） | utils.py 已過大，職責分離 |
| 是否 cache client | 是 vs 否 | 否 | STS temp credentials 有效期短，Lambda 冷啟動已經快，cache 增加複雜度 |
| 是否統一非 assume-role 的 S3 client | 是 vs 否 | 否（本輪） | 風險低、改動量大、可後續處理 |

## Out of Scope

- 統一所有 `boto3.client('s3')` 呼叫（不含 assume role 的場景）
- SFN / DynamoDB / CloudWatch 等其他 client 的統一
- client caching / connection pooling

## Test Plan

### Unit Tests（新增）

**檔案：** `tests/test_aws_clients.py`（新增）

| # | 測試 | 驗證 |
|---|------|------|
| T1 | `test_get_s3_client_no_role` | 不帶 role_arn → 直接 `boto3.client('s3')` |
| T2 | `test_get_s3_client_with_role` | 帶 role_arn → 先 STS assume_role → 用 temp creds 建 S3 client |
| T3 | `test_get_s3_client_session_name_truncation` | account_id 超長 → session name 截斷到 64 字 |
| T4 | `test_get_s3_client_with_region` | region 正確傳遞 |

### 回歸測試

- 既有 upload tests 全部通過（行為不變，只是呼叫入口統一）
- 既有 callback tests 全部通過

## Acceptance Criteria

- [ ] `aws_clients.py` 新增 `get_s3_client()` 函數
- [ ] `mcp_upload.py` 4 處 inline STS+S3 替換為 `get_s3_client()`
- [ ] `callbacks.py` 2 處 inline STS+S3 替換為 `get_s3_client()`
- [ ] 新增 `tests/test_aws_clients.py` 含 4 個測試
- [ ] 所有既有測試通過
- [ ] 淨減 ~80 行重複程式碼
