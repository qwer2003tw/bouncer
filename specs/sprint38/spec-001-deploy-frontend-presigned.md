# Spec: bouncer_deploy_frontend Presigned URL Refactor

**Task ID**: bouncer-s38-001
**Priority**: P0
**GitHub Issue**: #126
**Branch**: `feat/deploy-frontend-presigned-s38`

---

## Feature Summary

Refactor `bouncer_deploy_frontend` to use presigned S3 PUT URLs, bypassing API Gateway's 6MB payload limit and enabling direct client-to-S3 uploads for large frontend deployments.

### Problem Statement

Current implementation requires all base64-encoded file content to be sent through API Gateway → Lambda:
- API Gateway has a **6MB hard limit** on request payload size
- Large frontend bundles (e.g., React apps with assets) frequently exceed this limit
- No workaround exists without fundamentally changing the upload flow

### Solution Overview

Introduce a **two-step presigned URL workflow**:

1. **Step 1** (`bouncer_request_frontend_presigned`): Agent requests presigned PUT URLs for file metadata (no content)
2. **Agent uploads files**: Direct HTTP PUT to S3 using presigned URLs (bypasses API GW)
3. **Step 2** (`bouncer_confirm_frontend_deploy`): Agent confirms all files uploaded → triggers approval flow

---

## User Stories

### P1: Large File Upload Success
**As an** agent deploying a frontend bundle
**I want** to upload files larger than 6MB
**So that** I can deploy modern React apps with vendor bundles and assets

**Acceptance Criteria**:
- Successfully deploy a 20MB bundle (10 files × 2MB each)
- Presigned URL expiry: 300 seconds (5 minutes)
- All files uploaded via direct S3 PUT (no Lambda proxy)

### P2: Backward Compatibility
**As a** developer using existing tools
**I want** the old `bouncer_deploy_frontend` to continue working
**So that** migration to the new API can be gradual

**Acceptance Criteria**:
- Old tool marked as `deprecated` in TOOLS.md
- Returns a warning message: "Consider using bouncer_request_frontend_presigned for large files"
- Continues to function for files < 5MB total

### P3: Upload Verification
**As a** security reviewer
**I want** the confirm step to verify all files are present
**So that** incomplete uploads don't trigger approval requests

**Acceptance Criteria**:
- `bouncer_confirm_frontend_deploy` uses `head_object` to verify each file exists
- Fails with clear error if any file is missing: `"Missing files: [a.js, b.css]"`
- Verification completes in < 2 seconds for 50 files

---

## Acceptance Scenarios

### Scenario 1: Happy Path — Large Bundle Deploy

**Given**:
- Agent has a 15MB bundle (30 files)
- Project `ztp-files` is configured in `bouncer-projects` DynamoDB table

**When**:
1. Agent calls `bouncer_request_frontend_presigned`:
   ```json
   {
     "files": [
       {"filename": "index.html", "content_type": "text/html"},
       {"filename": "assets/main.js", "content_type": "application/javascript"},
       ...
     ],
     "project": "ztp-files",
     "reason": "Deploy v1.2.3",
     "source": "ztp-files-agent"
   }
   ```
2. Lambda returns presigned URLs:
   ```json
   {
     "status": "ready",
     "request_id": "frontend-abc123",
     "presigned_urls": [
       {
         "filename": "index.html",
         "url": "https://bouncer-uploads-123.s3.amazonaws.com/...",
         "s3_key": "frontend/ztp-files/frontend-abc123/index.html",
         "expires_at": "2026-03-14T03:00:00Z"
       },
       ...
     ]
   }
   ```
3. Agent uploads each file via HTTP PUT to presigned URL
4. Agent calls `bouncer_confirm_frontend_deploy(request_id="frontend-abc123")`

**Then**:
- Lambda verifies all 30 files exist via `head_object`
- DynamoDB `pending_approval` record created
- Telegram approval notification sent
- Returns:
  ```json
  {
    "status": "pending_approval",
    "request_id": "frontend-abc123",
    "message": "Deploy request sent. Use bouncer_status to poll."
  }
  ```

### Scenario 2: Presigned URL Expiry

**Given**:
- Agent received presigned URLs at T+0
- Files take 6 minutes to upload (network slow)

**When**:
- Agent attempts HTTP PUT at T+6min (after 300s expiry)

**Then**:
- S3 returns `403 Forbidden` with error: `Request has expired`
- Agent sees upload failure
- Agent must re-request presigned URLs via `bouncer_request_frontend_presigned` (generates new request_id)

### Scenario 3: Partial Upload Detection

**Given**:
- Agent uploaded 28 of 30 files
- Network error prevented upload of `assets/logo.png` and `robots.txt`

**When**:
- Agent calls `bouncer_confirm_frontend_deploy(request_id="frontend-abc123")`

**Then**:
- Lambda `head_object` verification fails
- Returns error:
  ```json
  {
    "status": "error",
    "error": "Upload incomplete. Missing files: [assets/logo.png, robots.txt]",
    "uploaded_count": 28,
    "expected_count": 30
  }
  ```
- No DynamoDB record created
- No Telegram notification sent

---

## Edge Cases

### EC1: Presigned URL Validation
- **Scenario**: Malicious agent requests presigned URLs for blocked extensions (`.exe`, `.php`)
- **Expected**: Validation in Step 1 rejects request with error: `"File 'malware.exe': blocked extension"`
- **Implementation**: Reuse existing `_has_blocked_extension()` from `mcp_deploy_frontend.py`

### EC2: head_object Failure (S3 Error)
- **Scenario**: S3 returns `500 InternalError` during `head_object` verification
- **Expected**: Lambda retries once (exponential backoff), then fails with: `"S3 verification error: InternalError"`
- **No partial state**: No DynamoDB write, no notification sent

### EC3: Duplicate request_id Collision
- **Scenario**: Agent retries Step 1 with identical file list, receives same `request_id`
- **Expected**: `request_id` is deterministic hash of `(project, timestamp, nonce)` — collision probability < 1e-9
- **Behavior**: New presigned URLs generated (overwrites existing S3 objects if present)

---

## Interface Contract

### New MCP Tool: `bouncer_request_frontend_presigned`

**Input Schema**:
```json
{
  "files": [
    {
      "filename": "string (required)",
      "content_type": "string (required)"
    }
  ],
  "project": "string (required)",
  "reason": "string (required)",
  "source": "string (optional)"
}
```

**Validation Rules**:
- `files` array: 1–200 items
- `filename`: No path traversal (`..`, leading `/`), no blocked extensions
- `content_type`: Non-empty string
- `project`: Must exist in `bouncer-projects` DynamoDB table with `frontend_bucket` field

**Output**:
```json
{
  "status": "ready",
  "request_id": "frontend-{uuid}",
  "presigned_urls": [
    {
      "filename": "string",
      "url": "string (presigned PUT URL)",
      "s3_key": "string (frontend/{project}/{request_id}/{filename})",
      "expires_at": "ISO 8601 timestamp",
      "method": "PUT",
      "headers": {"Content-Type": "string"}
    }
  ],
  "expires_in": 300
}
```

### New MCP Tool: `bouncer_confirm_frontend_deploy`

**Input Schema**:
```json
{
  "request_id": "string (required)"
}
```

**Output (Success)**:
```json
{
  "status": "pending_approval",
  "request_id": "string",
  "file_count": 30,
  "message": "Deploy request sent. Use bouncer_status to poll.",
  "expires_in": "600 seconds"
}
```

**Output (Verification Failure)**:
```json
{
  "status": "error",
  "error": "Upload incomplete. Missing files: [...]",
  "uploaded_count": 28,
  "expected_count": 30
}
```

---

## Security Section

### 1. Staging Bucket Policy (PUT-Only)

**Current State** (from template.yaml:418-424):
- Lambda has IAM permissions: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on `arn:aws:s3:::bouncer-uploads-{account_id}/*`
- No bucket-level policy restricting public access

**Required Change**:
Add bucket policy to `bouncer-uploads-{account_id}`:
```yaml
Statement:
  - Sid: DenyUnauthorizedGET
    Effect: Deny
    Principal: "*"
    Action:
      - s3:GetObject
      - s3:ListBucket
    Resource:
      - arn:aws:s3:::bouncer-uploads-{account_id}
      - arn:aws:s3:::bouncer-uploads-{account_id}/*
    Condition:
      StringNotEquals:
        aws:PrincipalArn: arn:aws:iam::{account_id}:role/bouncer-prod-function-role
```

**Rationale**:
- Presigned URLs allow PUT only (no GET/LIST capability)
- Prevents external enumeration of uploaded files
- Lambda retains full access for copy/delete operations

### 2. Presigned URL Scope

**Key Format**: `frontend/{project}/{request_id}/{filename}`
- `project`: Validated against `bouncer-projects` DynamoDB (whitelist)
- `request_id`: Lambda-generated UUID (no user control)
- `filename`: Sanitized via existing `sanitize_filename()` (removes `..`, `/`)

**Expiry**: 300 seconds (5 minutes)
- Limits time window for unauthorized PUT abuse
- Balances security vs. real-world upload times (assume 2MB/s = 10MB in 5s)

**Content-Type Enforcement**:
- Presigned URL includes `ContentType` parameter
- S3 rejects PUT if `Content-Type` header doesn't match

### 3. IAM Least Privilege

**Lambda Execution Role** (no changes required):
- Already has `s3:PutObject` for staging bucket (generates presigned URLs)
- No new permissions needed

**Frontend Deploy Role** (existing, no changes):
- `frontend_deploy_role_arn` (per-project) has `s3:PutObject` on production bucket
- Presigned URLs don't affect this role

---

## Implementation Notes

### DynamoDB Schema Changes

**None required** — presigned URLs use existing `pending_approval` schema:
- `request_id`: Generated in Step 1 (returned to agent)
- `files`: JSON array of `{filename, s3_key, content_type, cache_control, size}`
- `status`: Set to `pending_approval` only after Step 2 (confirm)

### Backward Compatibility Strategy

**Old Tool Deprecation** (`bouncer_deploy_frontend`):
1. Mark as `deprecated` in `TOOLS.md` (add warning banner)
2. Keep implementation unchanged (no code removal)
3. Add deprecation notice to response:
   ```json
   {
     "status": "pending_approval",
     "_warning": "This tool is deprecated. Use bouncer_request_frontend_presigned for files > 5MB."
   }
   ```
4. Set sunset date: Sprint 40 (2 sprints grace period)

### Migration Path for Agents

**Phase 1** (Sprint 38):
- New tools available
- Old tool still works
- Documentation updated with migration guide

**Phase 2** (Sprint 39):
- Log deprecation warnings for old tool usage (CloudWatch Logs)
- Slack notification to agent maintainers

**Phase 3** (Sprint 40):
- Remove old tool from TOOLS.md (breaking change announcement)

---

## Cost Analysis

### S3 Presigned PUT vs. Lambda Proxy

**Current (Lambda Proxy)**:
- API GW cost: $3.50/million requests
- Lambda cost: $0.20/GB-month (memory), $0.0000166667/GB-s (compute)
- Example: 10MB upload via Lambda (512MB memory, 2s duration):
  - Compute: 512MB × 2s × $0.0000166667/GB-s = $0.000017
  - Data transfer: Free (Lambda → S3 in same region)

**New (Presigned PUT)**:
- API GW cost: $3.50/million requests (Step 1 only, no payload)
- S3 PUT cost: $0.005/1000 PUT requests
- Example: 10MB upload via presigned URL (30 files):
  - Step 1 (metadata): $0.0000035
  - S3 PUT: 30 files × $0.005/1000 = $0.00015
  - Step 2 (confirm): $0.0000035
  - Total: **$0.0001585** (vs. $0.000017 for Lambda proxy)

**Cost Increase**: ~9× per deployment, but **negligible in absolute terms** ($0.00014 per deploy)

**Benefit**: Bypasses 6MB API GW limit — **operationally critical**, cost increase acceptable

---

## Test Strategy

### Unit Tests

**File**: `tests/test_mcp_deploy_frontend_presigned.py`

1. **Test: Presigned URL generation for valid metadata**
   - Input: 5 files with valid filenames/content_types
   - Assert: 5 presigned URLs returned, all expire in 300s

2. **Test: Blocked extension rejection**
   - Input: `files=[{filename: "hack.exe", ...}]`
   - Assert: Error returned, no presigned URLs generated

3. **Test: head_object verification success**
   - Mock: `s3.head_object()` returns `200 OK` for all files
   - Assert: Confirm returns `pending_approval`

4. **Test: head_object verification failure**
   - Mock: `s3.head_object()` raises `ClientError` (404 NotFound) for 2 files
   - Assert: Error lists missing files

### Integration Tests

**File**: `tests/integration/test_frontend_presigned_e2e.py`

1. **Test: Full upload → confirm → approve flow**
   - Step 1: Request presigned URLs (3 files)
   - Step 2: PUT files to S3 using `boto3.put_object()`
   - Step 3: Confirm deploy → verify Telegram notification sent
   - Step 4: Approve via callback → verify files copied to production bucket

2. **Test: Presigned URL expiry handling**
   - Step 1: Request presigned URLs
   - Step 2: Wait 301 seconds (simulate expiry)
   - Step 3: Attempt PUT → verify S3 returns 403
   - Step 4: Re-request presigned URLs → verify new URLs work

---

## Migration Notes (TOOLS.md Update)

### New Section: Frontend Deployment (Presigned URL)

```markdown
## bouncer_request_frontend_presigned

**Status**: ✅ Recommended for files > 5MB

Request presigned S3 PUT URLs for frontend deployment. Use this tool for large bundles that exceed API Gateway's 6MB limit.

**Workflow**:
1. Call `bouncer_request_frontend_presigned` with file metadata (no content)
2. Upload files directly to S3 using presigned URLs (HTTP PUT)
3. Call `bouncer_confirm_frontend_deploy` to trigger approval

**Example**:
```python
# Step 1: Request presigned URLs
response = bouncer.request_frontend_presigned(
    files=[
        {"filename": "index.html", "content_type": "text/html"},
        {"filename": "main.js", "content_type": "application/javascript"}
    ],
    project="ztp-files",
    reason="Deploy v1.2.3",
    source="my-agent"
)

# Step 2: Upload files
import requests
for file_info in response["presigned_urls"]:
    with open(file_info["filename"], "rb") as f:
        requests.put(file_info["url"], data=f, headers=file_info["headers"])

# Step 3: Confirm deployment
bouncer.confirm_frontend_deploy(request_id=response["request_id"])
```

## bouncer_deploy_frontend (Deprecated)

**Status**: ⚠️ Deprecated — Use `bouncer_request_frontend_presigned` for files > 5MB

Legacy tool that sends base64-encoded file content through API Gateway. Limited to ~5MB total payload size.

**Sunset Date**: Sprint 40 (2026-04-30)
```

---

## Open Questions

1. **Should we auto-cleanup expired presigned upload files?**
   - Option A: S3 lifecycle rule (delete `frontend/{project}/*` after 7 days)
   - Option B: Lambda cleanup cron (check for orphaned files daily)
   - **Decision**: Option A (lower operational cost, aligns with existing `pending/` prefix lifecycle)

2. **Should confirm step support partial retry?**
   - Scenario: Agent uploaded 28/30 files, got error, re-uploads 2 missing files
   - Current: Confirm checks all files from original metadata
   - Alternative: Accept `files` array in confirm step (override original list)
   - **Decision**: No — requires new request_id for security (prevents file swapping)

---

## References

- **GitHub Issue**: #126 (bouncer_deploy_frontend presigned URL refactor)
- **Related Code**: `src/mcp_deploy_frontend.py`, `src/mcp_presigned.py`
- **AWS Docs**: [Presigned URL Security](https://docs.aws.amazon.com/AmazonS3/latest/userguide/PresignedUrlUploadObject.html)
