# Implementation Plan: Sprint 33-002 sam package + DDB template_s3_url + IAM

> GitHub Issue: #120
> Tasks: sprint33-002 + sprint33-003

---

## Technical Context

### 影響檔案

| 檔案 | 變更類型 | 說明 |
|------|----------|------|
| `deployer/scripts/sam_deploy.py` | 修改 | 加 sam package 步驟、DDB update 邏輯 |
| `deployer/template.yaml` | 修改（IAM） | CodeBuildRole 新增 DDB UpdateItem |
| `deployer/template.yaml` | 修改（buildspec） | 傳遞 PROJECT_ID 環境變數給 CodeBuild |
| `tests/test_sam_deploy.py`（或新建） | 新增測試 | sam package + DDB update 路徑 |

### 現狀 sam_deploy.py 流程

```
main()
  ↓ _check_github_pat()
  ↓ _build_sam_cmd()  → ["sam", "deploy", "--resolve-s3", ...]
  ↓ _run_deploy(cmd)  → subprocess run
  ↓ if succeeded: sys.exit(0)
  ↓ else: conflict check + optional import + retry
```

### 目標流程

```
main()
  ↓ _check_github_pat()
  ↓ _run_sam_package()   ← NEW: sam package → /tmp/{stack}-packaged.yaml
  ↓ _build_deploy_cmd()  ← 移除 --resolve-s3, 改用 --template-file
  ↓ _run_deploy(cmd)
  ↓ if succeeded:
      ↓ _update_template_s3_url()  ← NEW: boto3 DDB update (best-effort)
      ↓ sys.exit(0)
  ↓ else: conflict check + optional import + retry
      ↓ if retry succeeded:
          ↓ _update_template_s3_url()  ← 也更新
          ↓ sys.exit(0)
```

### Cross-account DDB 更新重要設計決策

**問題**：CodeBuild buildspec 在 cross-account 部署時會 `assume-role` 替換 AWS credentials，之後的 boto3 呼叫會以 cross-account role 身份執行。ProjectsTable 在**主帳號**，cross-account role 沒有（也不應有）主帳號 DDB 權限。

**解決方案**：`_update_template_s3_url()` 必須在 assume-role **之前**執行。

但目前流程：`sam_deploy.py` 本身是在 assume-role **之後**才被呼叫（buildspec 先 assume-role，再 `python3 /tmp/sam_deploy.py`）。

**選項 A**（推薦）：sam_deploy.py 接受一個 `--main-account-ddb-table` 環境變數，在建立 boto3 client 時不使用當前 session（可能是 cross-account），而是用 CodeBuild **原始** role（透過 `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` — CodeBuild 原生 credentials，不受 assume-role 影響，因為 assume-role 是 shell export）。

實際上：`os.environ.get("AWS_ACCESS_KEY_ID")` 等 env vars 被 assume-role 覆蓋了，但 **EC2 instance profile / CodeBuild metadata credentials** 仍可透過 **不帶 env var 的新 session** 取得。

**最簡方案（選項 B）**：在 buildspec 中，assume-role 之前先呼叫 DDB update（而非在 sam_deploy.py 中）。但這需要修改 buildspec，且邏輯散落在兩處。

**決策**：採用**選項 A**，sam_deploy.py 中使用 `botocore.session` 建立不帶 explicit credentials 的 client（讓它從 metadata service 取 CodeBuild role 的 credentials，不受 env var 污染）。

```python
import botocore.session

def _update_template_s3_url(stack: str, project_id: str, bucket: str, region: str) -> None:
    """Best-effort: update ProjectsTable.template_s3_url after successful deploy."""
    table_name = os.environ.get('PROJECTS_TABLE', 'bouncer-projects')
    if not project_id:
        print("[ddb] PROJECT_ID not set, skipping template_s3_url update")
        return
    
    s3_url = f"https://{bucket}.s3.{region}.amazonaws.com/templates/{stack}-packaged.yaml"
    
    try:
        # Use fresh botocore session to avoid cross-account assumed role credentials
        session = botocore.session.get_session()
        ddb = session.create_client('dynamodb', region_name=region)
        ddb.update_item(
            TableName=table_name,
            Key={'project_id': {'S': project_id}},
            UpdateExpression='SET template_s3_url = :url',
            ExpressionAttributeValues={':url': {'S': s3_url}},
        )
        print(f"[ddb] Updated template_s3_url for {project_id}: {s3_url}")
    except Exception as e:
        print(f"[ddb] WARNING: Failed to update template_s3_url (ignored): {e}")
```

> **Note**: 如果 CodeBuild 沒有在 env 設定 `PROJECTS_TABLE`，預設 fallback 到 `bouncer-projects`（template.yaml 的 table name）。

---

## Constitution Check

| 面向 | 評估 |
|------|------|
| 安全影響 | 低。CodeBuildRole 新增 DDB UpdateItem 權限，範圍限定 ProjectsTable ARN |
| 成本影響 | 微量（每次 deploy 多 1 DDB UpdateItem + sam package S3 upload） |
| 架構影響 | 中。sam_deploy.py 邏輯變化（新增 package 步驟），需 deployer stack 重新部署（IAM 變更） |
| 向後相容 | `ARTIFACTS_BUCKET` 未設定時 fallback `--resolve-s3`，不破壞現有行為 |

---

## Implementation Phases

### Phase 1: sam_deploy.py — 新增 _run_sam_package()

```python
def _run_sam_package(stack: str, artifacts_bucket: str, region: str) -> str:
    """Run sam package, return packaged template local path."""
    output_template = f"/tmp/{stack}-packaged.yaml"
    cmd = [
        "sam", "package",
        "--template-file", "template.yaml",
        "--output-template-file", output_template,
        "--s3-bucket", artifacts_bucket,
        "--s3-prefix", "templates",
        "--region", region,
    ]
    print(f"[package] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False, text=True)
    if result.returncode != 0:
        print(f"ERROR: sam package failed (rc={result.returncode})", file=sys.stderr)
        sys.exit(result.returncode)
    return output_template
```

### Phase 2: 修改 _build_sam_cmd() → 接受 template_file 參數

移除 `"--resolve-s3"` 行，改為：

```python
if template_file:
    cmd.extend(["--template-file", template_file])
else:
    cmd.append("--resolve-s3")  # fallback
```

### Phase 3: 修改 main() 整合

```python
artifacts_bucket = os.environ.get("ARTIFACTS_BUCKET", "").strip()
project_id = os.environ.get("PROJECT_ID", "").strip()
region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1").strip()

template_file = None
if artifacts_bucket:
    template_file = _run_sam_package(stack, artifacts_bucket, region)
else:
    print("[package] ARTIFACTS_BUCKET not set, using --resolve-s3 fallback")

cmd = _build_sam_cmd(stack, params_raw, cfn_role, target_role, template_file=template_file)
```

成功後：
```python
if deploy_result.succeeded:
    if artifacts_bucket and project_id:
        _update_template_s3_url(stack, project_id, artifacts_bucket, region)
    sys.exit(0)
```

### Phase 4: template.yaml — IAM 修改（CodeBuildRole）

在 `Sid: S3Artifacts` 之後新增：

```yaml
              - Sid: DDBProjectsTable
                Effect: Allow
                Action:
                  - dynamodb:UpdateItem
                Resource:
                  - !GetAtt ProjectsTable.Arn
```

### Phase 5: template.yaml — buildspec 傳遞 PROJECT_ID

在 Step Function `StartBuild` EnvironmentVariablesOverride 新增：

```yaml
                - Name: PROJECT_ID
                  Type: PLAINTEXT
                  Value.$: $.project_id
```

### Phase 6: 測試

測試檔：`tests/test_sprint33_002_sam_package_ddb.py`

測試案例：
- `test_sam_package_called_when_artifacts_bucket_set`
- `test_sam_deploy_uses_packaged_template_not_resolve_s3`
- `test_ddb_update_called_after_successful_deploy`
- `test_ddb_update_failure_does_not_fail_deploy`
- `test_fallback_to_resolve_s3_when_no_artifacts_bucket`
- `test_project_id_not_set_skips_ddb_update`

---

## Deployment Note

IAM 變更 (`CodeBuildRole` 新增 DDB permission) + buildspec 變更 (PROJECT_ID 傳遞) 需要 **deployer stack** 重新部署（`bouncer-deployer` CloudFormation stack）。

sam_deploy.py script 更新則需要 `make -C deployer upload-deploy-script`（上傳到 S3 `ARTIFACTS_BUCKET/deployer-scripts/sam_deploy.py`）。
