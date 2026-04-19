# Implementation Plan: Deploy Frontend Presigned URL Refactor

**Task**: bouncer-s38-001
**Estimated Effort**: 12–16 hours
**Priority**: P0

---

## Technical Context

### Existing Code Path (Current Flow)

**Entry Point**: `src/mcp_deploy_frontend.py::mcp_tool_deploy_frontend()`

**Step-by-step flow**:
1. Agent calls MCP tool `bouncer_deploy_frontend` with files array:
   ```json
   {
     "files": [
       {"filename": "index.html", "content": "<base64>", "content_type": "text/html"}
     ],
     "project": "ztp-files",
     "reason": "Deploy v1.2.3"
   }
   ```
2. Lambda validates:
   - `_validate_files()`: Check file count, size limits, blocked extensions, base64 decoding
   - `_get_project_config()`: Verify project exists in `bouncer-projects` DynamoDB
3. Lambda decodes base64 content and uploads to staging bucket:
   - Bucket: `bouncer-uploads-{account_id}`
   - Key: `pending/{request_id}/{filename}`
   - Uses `boto3.s3.put_object()` directly
4. Lambda writes DynamoDB `pending_approval` record
5. Lambda sends Telegram notification via `send_deploy_frontend_notification()`
6. Returns `pending_approval` status to agent

**Callback flow** (after approval):
- `src/callbacks.py::handle_deploy_frontend_callback()`
- Lambda copies files from staging → production bucket
- CloudFront invalidation
- Cleanup staging files

### Key Files and Functions

**Files**:
- `src/mcp_deploy_frontend.py` (685 lines) — main tool implementation
- `src/mcp_presigned.py` (612 lines) — presigned URL helper (existing, for uploads)
- `src/mcp_tools.py` (152 lines) — tool registry (re-exports)
- `src/callbacks.py` (1588+ lines) — approval callback handlers
- `template.yaml` — CloudFormation (IAM policies, S3 bucket)

**Functions to reference**:
- `_validate_files(files: list) -> Optional[str]` (mcp_deploy_frontend.py:181-249)
- `_get_cache_control(filename: str) -> str` (mcp_deploy_frontend.py:149-156)
- `_get_content_type(filename, provided_ct) -> str` (mcp_deploy_frontend.py:159-166)
- `_generate_presigned_url_for_file()` (mcp_presigned.py:53-78) — existing helper

**DynamoDB Schema** (`bouncer-{env}-requests` table):
- `request_id` (PK): `frontend-{uuid}`
- `action`: `deploy_frontend`
- `status`: `pending_approval` | `approved` | `denied` | `expired`
- `files`: JSON string `[{filename, s3_key, content_type, cache_control, size}]`
- `project`, `frontend_bucket`, `distribution_id`, `region`, `deploy_role_arn`
- `staging_bucket`: `bouncer-uploads-{account_id}`
- `created_at`, `ttl`, `mode`: `mcp`

---

## Implementation Phases

### Phase 0: Prerequisite Refactoring (2 hours)

**Goal**: Extract shared helpers to avoid code duplication

**Tasks**:
1. Create `src/mcp_deploy_frontend_shared.py`:
   - Extract `_validate_files()` (rename to `validate_file_metadata()` — no base64 decoding)
   - Extract `_get_cache_control()`, `_get_content_type()` (no changes)
   - Extract `_has_blocked_extension()` (no changes)
2. Update `src/mcp_deploy_frontend.py` to import from shared module
3. Run existing tests to verify no regressions

**Acceptance**:
- All existing tests pass
- No functional changes to `bouncer_deploy_frontend` tool

---

### Phase 1: New Tool — `bouncer_request_frontend_presigned` (4 hours)

**Goal**: Generate presigned PUT URLs for file metadata (no upload)

**File**: `src/mcp_deploy_frontend_presigned.py` (new file)

**Implementation**:

```python
"""Presigned URL-based frontend deployment (Step 1: request URLs)"""

import time
from typing import Optional
from dataclasses import dataclass, field

from utils import generate_request_id, mcp_result
from aws_clients import get_s3_client
from mcp_deploy_frontend_shared import (
    validate_file_metadata,
    _get_project_config,
    _list_known_projects,
)
from constants import DEFAULT_ACCOUNT_ID


@dataclass
class FrontendPresignedContext:
    """Context for presigned URL request"""
    req_id: str
    files: list  # [{filename, content_type}]
    project: str
    reason: str
    source: str
    # Resolved after validation
    request_id: str = field(default="")
    project_config: dict = field(default_factory=dict)


def mcp_tool_request_frontend_presigned(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_request_frontend_presigned

    Returns presigned PUT URLs for frontend deployment files.
    """
    # 1. Parse and validate arguments
    files = arguments.get("files", [])
    project = str(arguments.get("project", "")).strip()
    reason = str(arguments.get("reason", "")).strip()
    source = arguments.get("source", "")

    if not project:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": "project is required"
            })}],
            "isError": True,
        })

    # 2. Validate project exists
    project_config = _get_project_config(project)
    if not project_config:
        available = _list_known_projects()
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": f"Unknown project: {project}",
                "available_projects": available,
            })}],
            "isError": True,
        })

    # 3. Validate file metadata (no base64 content)
    error = validate_file_metadata(files)
    if error:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": error,
            })}],
            "isError": True,
        })

    # 4. Generate request_id
    request_id = generate_request_id(f"deploy_frontend:{project}")

    # 5. Generate presigned URLs for each file
    s3 = get_s3_client()
    staging_bucket = f"bouncer-uploads-{DEFAULT_ACCOUNT_ID}"
    presigned_urls = []

    for f in files:
        filename = f["filename"]
        content_type = f.get("content_type", "application/octet-stream")
        s3_key = f"frontend/{project}/{request_id}/{filename}"

        try:
            url = s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": staging_bucket,
                    "Key": s3_key,
                    "ContentType": content_type,
                },
                ExpiresIn=300,  # 5 minutes
            )
        except ClientError as exc:
            return mcp_result(req_id, {
                "content": [{"type": "text", "text": json.dumps({
                    "status": "error",
                    "error": f"Failed to generate presigned URL for {filename}: {exc}"
                })}],
                "isError": True,
            })

        presigned_urls.append({
            "filename": filename,
            "url": url,
            "s3_key": s3_key,
            "s3_uri": f"s3://{staging_bucket}/{s3_key}",
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 300)),
            "method": "PUT",
            "headers": {"Content-Type": content_type},
        })

    # 6. Return presigned URLs (no DDB write, no notification)
    return mcp_result(req_id, {
        "content": [{"type": "text", "text": json.dumps({
            "status": "ready",
            "request_id": request_id,
            "presigned_urls": presigned_urls,
            "expires_in": 300,
        })}],
    })
```

**Tests** (`tests/test_mcp_deploy_frontend_presigned.py`):
1. Test: Valid metadata returns presigned URLs
2. Test: Blocked extension rejection
3. Test: Unknown project error
4. Test: Presigned URL format validation (S3 signature present)

---

### Phase 2: New Tool — `bouncer_confirm_frontend_deploy` (4 hours)

**Goal**: Verify uploaded files and trigger approval flow

**File**: `src/mcp_deploy_frontend_confirm.py` (new file)

**Implementation**:

```python
"""Presigned URL-based frontend deployment (Step 2: confirm upload)"""

import json
import time
from botocore.exceptions import ClientError

from aws_clients import get_s3_client
from constants import DEFAULT_ACCOUNT_ID, UPLOAD_TIMEOUT, APPROVAL_TTL_BUFFER
from db import table
from notifications import send_deploy_frontend_notification
from utils import mcp_result


def mcp_tool_confirm_frontend_deploy(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_confirm_frontend_deploy

    Verifies all files uploaded via presigned URLs, then submits for approval.
    """
    # 1. Parse arguments
    request_id = str(arguments.get("request_id", "")).strip()

    if not request_id:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": "request_id is required"
            })}],
            "isError": True,
        })

    # 2. Reconstruct file list from request_id pattern
    # NOTE: This requires storing metadata in a temporary table or deriving from presigned keys
    # For MVP, we can require agent to pass `files` metadata again (simpler)
    files_metadata = arguments.get("files", [])
    project = arguments.get("project", "")
    reason = arguments.get("reason", "")
    source = arguments.get("source", "")

    if not files_metadata or not project:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": "files and project metadata required for confirmation"
            })}],
            "isError": True,
        })

    # 3. Verify all files exist in S3 using head_object
    s3 = get_s3_client()
    staging_bucket = f"bouncer-uploads-{DEFAULT_ACCOUNT_ID}"
    missing_files = []
    uploaded_files = []

    for f in files_metadata:
        filename = f["filename"]
        s3_key = f"frontend/{project}/{request_id}/{filename}"

        try:
            response = s3.head_object(Bucket=staging_bucket, Key=s3_key)
            uploaded_files.append({
                "filename": filename,
                "s3_key": s3_key,
                "content_type": f.get("content_type"),
                "size": response.get("ContentLength", 0),
            })
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "404":
                missing_files.append(filename)
            else:
                return mcp_result(req_id, {
                    "content": [{"type": "text", "text": json.dumps({
                        "status": "error",
                        "error": f"S3 verification error: {exc}"
                    })}],
                    "isError": True,
                })

    if missing_files:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": f"Upload incomplete. Missing files: {missing_files}",
                "uploaded_count": len(uploaded_files),
                "expected_count": len(files_metadata),
            })}],
            "isError": True,
        })

    # 4. Get project config (same as old flow)
    from mcp_deploy_frontend_shared import _get_project_config
    project_config = _get_project_config(project)

    # 5. Write DynamoDB pending_approval record
    ttl = int(time.time()) + UPLOAD_TIMEOUT + APPROVAL_TTL_BUFFER
    item = {
        "request_id": request_id,
        "action": "deploy_frontend",
        "status": "pending_approval",
        "project": project,
        "frontend_bucket": project_config["frontend_bucket"],
        "distribution_id": project_config["distribution_id"],
        "region": project_config.get("region", "us-east-1"),
        "deploy_role_arn": project_config.get("deploy_role_arn"),
        "staging_bucket": staging_bucket,
        "files": json.dumps(uploaded_files),
        "file_count": len(uploaded_files),
        "total_size": sum(f["size"] for f in uploaded_files),
        "reason": reason,
        "source": source or "__anonymous__",
        "created_at": int(time.time()),
        "ttl": ttl,
        "mode": "mcp",
    }
    table.put_item(Item=item)

    # 6. Send Telegram approval notification
    target_info = {
        "frontend_bucket": project_config["frontend_bucket"],
        "distribution_id": project_config["distribution_id"],
        "region": project_config.get("region", "us-east-1"),
    }

    notif_result = send_deploy_frontend_notification(
        request_id=request_id,
        files_summary=uploaded_files,
        target_info=target_info,
        project=project,
        reason=reason,
        source=source,
    )

    if not notif_result.ok:
        # Cleanup DDB on notification failure
        table.delete_item(Key={"request_id": request_id})
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": "Telegram notification failed; deploy request was not created."
            })}],
            "isError": True,
        })

    # 7. Store telegram_message_id for later cleanup
    if notif_result.message_id:
        from notifications import post_notification_setup
        post_notification_setup(
            request_id=request_id,
            telegram_message_id=notif_result.message_id,
            expires_at=ttl,
        )

    # 8. Return success
    return mcp_result(req_id, {
        "content": [{"type": "text", "text": json.dumps({
            "status": "pending_approval",
            "request_id": request_id,
            "file_count": len(uploaded_files),
            "message": "Frontend deploy request sent. Use bouncer_status to poll.",
            "expires_in": f"{UPLOAD_TIMEOUT} seconds",
        })}],
    })
```

**Tests** (`tests/test_mcp_deploy_frontend_confirm.py`):
1. Test: All files uploaded → returns `pending_approval`
2. Test: Missing files → returns error with file list
3. Test: S3 head_object failure → returns error

---

### Phase 3: Deprecate Old Tool (1 hour)

**Goal**: Mark `bouncer_deploy_frontend` as deprecated

**Changes**:
1. Update `TOOLS.md`:
   - Add deprecation warning to `bouncer_deploy_frontend` section
   - Add new section for `bouncer_request_frontend_presigned` + `bouncer_confirm_frontend_deploy`
2. Update `src/mcp_deploy_frontend.py` response:
   - Add `"_warning": "Deprecated. Use bouncer_request_frontend_presigned for files > 5MB"`
3. Update skill docs (if applicable)

**No code removal** — old tool remains functional for backward compatibility.

---

### Phase 4: Security — Staging Bucket Policy (2 hours)

**Goal**: Restrict staging bucket access to PUT-only via presigned URLs

**WARNING**: This phase requires **careful testing** to avoid breaking existing upload flows.

**Changes**:
1. Update `template.yaml`:
   - Add `StagingBucket` resource (currently implicit)
   - Add bucket policy (DenyUnauthorizedGET)
2. Test existing `bouncer_upload` and `bouncer_request_presigned` tools
3. Verify presigned URLs still work (PUT allowed, GET denied)

**Implementation** (template.yaml):
```yaml
Resources:
  StagingBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Sub "bouncer-uploads-${DefaultAccountId}"
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
      LifecycleConfiguration:
        Rules:
          - Id: CleanupFrontendUploads
            Status: Enabled
            Prefix: frontend/
            ExpirationInDays: 7
      Tags:
        - Key: Project
          Value: Bouncer
        - Key: auto-delete
          Value: "no"

  StagingBucketPolicy:
    Type: AWS::S3::BucketPolicy
    Properties:
      Bucket: !Ref StagingBucket
      PolicyDocument:
        Statement:
          - Sid: AllowLambdaFullAccess
            Effect: Allow
            Principal:
              AWS: !GetAtt ApprovalFunction.Arn
            Action:
              - s3:PutObject
              - s3:GetObject
              - s3:DeleteObject
              - s3:ListBucket
            Resource:
              - !Sub "arn:aws:s3:::bouncer-uploads-${DefaultAccountId}"
              - !Sub "arn:aws:s3:::bouncer-uploads-${DefaultAccountId}/*"
          - Sid: DenyUnauthorizedGET
            Effect: Deny
            Principal: "*"
            Action:
              - s3:GetObject
              - s3:ListBucket
            Resource:
              - !Sub "arn:aws:s3:::bouncer-uploads-${DefaultAccountId}"
              - !Sub "arn:aws:s3:::bouncer-uploads-${DefaultAccountId}/*"
            Condition:
              StringNotLike:
                aws:PrincipalArn:
                  - !Sub "arn:aws:iam::${AWS::AccountId}:role/bouncer-*-function-role"
```

**Risk**: Existing `bouncer_upload` tool also uses staging bucket — verify it's not affected.

---

### Phase 5: Integration Testing (2 hours)

**Goal**: End-to-end validation of new workflow

**Test Scenarios**:
1. **Happy path**: 30 files, 15MB total
   - Step 1: Request presigned URLs
   - Step 2: Upload files via HTTP PUT (using `requests` library)
   - Step 3: Confirm deploy
   - Step 4: Approve via Telegram
   - Verify: Files copied to production bucket
2. **Expiry test**: Request URLs → wait 6 minutes → attempt upload
   - Verify: S3 returns 403
3. **Partial upload test**: Upload 28/30 files → confirm
   - Verify: Error with missing file list

**Tools**:
- `tests/integration/test_frontend_presigned_e2e.py`
- Use `moto` for S3 mocking (or real S3 bucket in dev account)

---

## Test Strategy

### Unit Tests (Phase 1-2)

**Coverage targets**:
- `mcp_tool_request_frontend_presigned`: 90%+
- `mcp_tool_confirm_frontend_deploy`: 90%+
- Shared helpers: 100% (reuse existing tests)

**Key test cases**:
- Valid metadata → presigned URLs generated
- Blocked extension → error
- Unknown project → error
- All files uploaded → pending_approval
- Missing files → error with file list
- S3 head_object failure → error

### Integration Tests (Phase 5)

**End-to-end flows**:
- Full deploy (request → upload → confirm → approve)
- Presigned URL expiry (request → wait → upload fails)
- Partial upload (request → upload partial → confirm fails)

**Environment**:
- Use `moto` for S3/DDB mocking (faster, no AWS costs)
- Or: dev account with real S3 bucket (slower, more realistic)

### Manual Tests (Post-deployment)

**Checklist**:
1. Deploy to staging environment
2. Test with real frontend bundle (e.g., ztp-files v1.2.3)
3. Verify Telegram notifications render correctly
4. Test approval callback (approve/deny)
5. Verify CloudFront invalidation
6. Check CloudWatch Logs for errors

---

## Migration Notes

### TOOLS.md Update

**New section** (add before old `bouncer_deploy_frontend` section):

```markdown
## bouncer_request_frontend_presigned

**Status**: ✅ Recommended for files > 5MB

Request presigned S3 PUT URLs for frontend deployment. Bypasses API Gateway's 6MB payload limit.

**Step 1: Request presigned URLs**
- Input: File metadata (no content)
- Output: Presigned PUT URLs (valid for 5 minutes)

**Step 2: Upload files**
- Use HTTP PUT to upload files directly to S3
- Bypasses API Gateway (no size limit)

**Step 3: Confirm deployment**
- Call `bouncer_confirm_frontend_deploy` to trigger approval

**Example** (see spec-001 for full code)

## bouncer_deploy_frontend (Deprecated)

**Status**: ⚠️ Deprecated — Use `bouncer_request_frontend_presigned` for files > 5MB

Legacy tool that sends base64-encoded file content through API Gateway. Limited to ~5MB total.

**Sunset date**: Sprint 40 (2026-04-30)
```

---

## Cost Analysis

### New vs. Old Cost Breakdown

**Old flow** (Lambda proxy):
- API GW: $3.50/million requests
- Lambda compute: ~$0.000017 per 10MB upload

**New flow** (presigned URL):
- API GW (Step 1): $3.50/million requests (metadata only)
- S3 PUT: $0.005/1000 requests (30 files = $0.00015)
- API GW (Step 3): $3.50/million requests (confirm)
- Total: ~$0.0001585 per deploy

**Cost increase**: ~9× per deployment, but **$0.00014 absolute increase** (negligible)

**Operational benefit**: Bypasses 6MB API GW limit → **critical for usability**

---

## Rollback Plan

### If Issues Arise Post-Deployment

**Scenario 1**: Presigned URLs fail (S3 permissions issue)
- **Action**: Revert IAM policy changes
- **Impact**: New tool unavailable, old tool still works
- **Timeline**: 15 minutes (SAM deploy rollback)

**Scenario 2**: Staging bucket policy breaks existing uploads
- **Action**: Remove bucket policy, revert to IAM-only access control
- **Impact**: Presigned URLs still work, but less secure
- **Timeline**: 15 minutes

**Scenario 3**: DDB schema incompatibility
- **Action**: No schema changes required (same `pending_approval` record)
- **Impact**: None (new tool uses existing schema)

---

## Open Questions

1. **Should we store presigned URL metadata in DDB?**
   - **Current plan**: No (agent must pass `files` metadata again in confirm step)
   - **Alternative**: Write temp record in Step 1, read in Step 2 (more stateful)
   - **Decision**: Current plan (simpler, agent-side deduplication)

2. **Should we auto-cleanup expired presigned upload files?**
   - **Proposed**: S3 lifecycle rule (delete `frontend/{project}/*` after 7 days)
   - **Risk**: None (files in `frontend/` prefix are always temporary)
   - **Decision**: Add lifecycle rule in Phase 4

3. **Should we add file size limit per-file?**
   - **Current**: No per-file limit (only total size limit in old tool)
   - **Proposed**: 50MB per-file limit (reasonable for frontend assets)
   - **Decision**: Add in Step 1 validation (reuse `MAX_FILE_SIZE_BYTES`)

---

## Success Metrics

**Definition of Done**:
- ✅ All unit tests pass (90%+ coverage)
- ✅ All integration tests pass
- ✅ Manual testing completed (checklist above)
- ✅ TOOLS.md updated with deprecation notice
- ✅ No regressions in existing `bouncer_deploy_frontend` tool
- ✅ Successfully deployed 20MB bundle via new workflow

**Performance targets**:
- Presigned URL generation: < 1s for 50 files
- File verification (head_object): < 2s for 50 files
- End-to-end deploy (request → upload → confirm → approve): < 5 minutes

---

## References

- **Spec**: `specs/sprint38/spec-001-deploy-frontend-presigned.md`
- **GitHub Issue**: #126
- **Related Files**: `src/mcp_deploy_frontend.py`, `src/mcp_presigned.py`, `template.yaml`
