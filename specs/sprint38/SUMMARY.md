# Sprint 38 Spec Summary

**Generated**: 2026-03-14
**Status**: ✅ Complete
**Total Effort**: 20–24 hours (2-week sprint)

---

## Deliverables

### Spec Files
1. **spec-001-deploy-frontend-presigned.md** — Presigned URL refactor (P0, #126)
2. **spec-002-button-labels-english.md** — Button labels localization (P1, #46)
3. **spec-003-button-colors.md** — Button visual distinction (P1, #41)
4. **plan-001-deploy-frontend-presigned.md** — Implementation plan (#126)
5. **tasks.md** — Sprint task breakdown + registration commands

---

## Key Design Decisions

### #126: bouncer_deploy_frontend Presigned URL Refactor (P0)

#### 1. Two-Step Presigned URL Architecture

**Decision**: Split deploy into two MCP tools:
- **Step 1** (`bouncer_request_frontend_presigned`): Agent requests presigned PUT URLs (no file content)
- **Agent uploads**: Direct HTTP PUT to S3 (bypasses API Gateway)
- **Step 2** (`bouncer_confirm_frontend_deploy`): Agent confirms upload → triggers approval

**Rationale**:
- Bypasses API Gateway 6MB payload limit (critical blocker)
- Presigned URLs eliminate need for Lambda to proxy file data
- Agent has full control over upload retry logic

**Alternative considered**: Single-step presigned URL + auto-detection of uploads
- ❌ Rejected: Requires polling or complex state management

#### 2. Presigned URL Expiry: 300 Seconds (5 Minutes)

**Decision**: Fixed 300-second expiry for all presigned URLs

**Rationale**:
- Balances security (short time window) vs. usability (realistic upload times)
- Assumption: 2MB/s upload speed → 10MB in 5 seconds (100× safety margin)
- Agent can re-request URLs if expiry occurs (generates new request_id)

**Alternative considered**: User-configurable expiry (60–3600s)
- ❌ Rejected: Increases security risk, adds complexity

#### 3. Staging Key Format: `frontend/{project}/{request_id}/{filename}`

**Decision**: Prefix staging uploads with `frontend/` (not `pending/`)

**Rationale**:
- Isolates frontend deploys from command uploads (`pending/` prefix)
- Enables S3 lifecycle rule: cleanup `frontend/` after 7 days
- No DDB write in Step 1 → key format is the only state identifier

**Alternative considered**: Reuse `pending/` prefix
- ❌ Rejected: Mixes two different upload workflows (confusing lifecycle rules)

#### 4. Confirm Step Requires File Metadata (Not Just request_id)

**Decision**: `bouncer_confirm_frontend_deploy` accepts `files` metadata array (not just `request_id`)

**Rationale**:
- No persistent state in Step 1 (no DDB write) → agent must re-supply metadata
- Simpler implementation (no temp table for presigned URL state)
- Agent already has file list (passed to Step 1) → minimal redundancy

**Alternative considered**: Store metadata in DDB during Step 1
- ❌ Rejected: Adds state management, increases DDB writes (cost/complexity)

#### 5. Verification Uses `head_object` (Not `get_object`)

**Decision**: Confirm step uses S3 `head_object()` to verify file existence

**Rationale**:
- No need to download file content (faster, cheaper)
- `head_object` returns `ContentLength` (validates file not empty)
- Completes in < 2 seconds for 50 files (acceptable latency)

**Alternative considered**: Download + hash verification
- ❌ Rejected: Slow (downloads all files), expensive (data transfer), overkill for MVP

#### 6. Staging Bucket Policy: PUT-Only Presigned URLs

**Decision**: Add S3 bucket policy denying GET/LIST for non-Lambda principals

**Security rationale**:
- Presigned URLs grant PUT permission only (no GET/LIST)
- Prevents external enumeration of uploaded files
- Lambda retains full access (PutObject/GetObject/DeleteObject) via IAM role

**Implementation**:
```yaml
Statement:
  - Sid: DenyUnauthorizedGET
    Effect: Deny
    Principal: "*"
    Action: [s3:GetObject, s3:ListBucket]
    Condition:
      StringNotLike:
        aws:PrincipalArn: "arn:aws:iam::*:role/bouncer-*-function-role"
```

**Alternative considered**: IAM-only access control (no bucket policy)
- ❌ Rejected: Weaker security (presigned URLs could leak GET access)

#### 7. Backward Compatibility: Deprecate, Don't Remove

**Decision**: Keep old `bouncer_deploy_frontend` tool, mark as deprecated

**Sunset timeline**:
- **Sprint 38**: Add deprecation warning to TOOLS.md + tool response
- **Sprint 39**: Log usage metrics, notify agent maintainers
- **Sprint 40**: Remove from TOOLS.md (breaking change announcement)

**Rationale**:
- Gradual migration reduces risk of breaking existing agents
- 2-sprint grace period allows for coordinated updates

---

### #46: Localize Telegram Button Labels to English (P1)

#### 8. Scope Clarification Required: Buttons vs. Message Bodies

**Finding**: All inline keyboard button labels are **already in English**

**Audit results** (notifications.py):
- ✅ `'✅ Approve'`, `'❌ Reject'`, `'🔓 Trust 10min'` (all English)
- ✅ No Chinese button text found

**Open question**: What is the actual scope of #46?
- **Option A**: Issue already resolved (close as complete)
- **Option B**: Issue refers to **message body text** (extensive Chinese: "來源："、"帳號："、"命令：" etc.)
- **Option C**: Issue refers to specific flows not yet audited (deployer, callbacks)

**Recommendation**: Clarify with product owner before proceeding
- If scope = buttons only → **0 hours** (already complete)
- If scope = message bodies → **12 hours** (full i18n refactor)

**Implementation plan** (if scope = message bodies):
1. Create `src/i18n/en.py` language file
2. Refactor `notifications.py` to use language strings
3. Add config flag: `NOTIFICATION_LANGUAGE=en`

---

### #41: Inline Keyboard Button Visual Distinction (P1)

#### 9. Telegram API Limitation: No Native Button Colors

**Analysis**: Telegram Bot API **does not support button colors**
- `'style': 'success' | 'danger' | 'primary'` attribute is **stripped** by `_strip_unsupported_button_fields()`
- Only text, callback_data, URL are supported

**Reference**: [Telegram Bot API InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton)

#### 10. Solution: Color-Coded Emoji Prefixes

**Decision**: Replace semantic emoji with colored circle emoji

| Action | Current | New | Color Semantic |
|--------|---------|-----|----------------|
| Approve | ✅ Checkmark | 🟢 Green Circle | Safe / Proceed |
| Deny | ❌ X Mark | 🔴 Red Circle | Danger / Stop |
| Trust | 🔓 Unlock | 🔵 Blue Circle | Trust / Elevated |

**Rationale**:
- **Colored circles** = universal traffic light pattern (🟢 go, 🔴 stop)
- **Higher contrast** than semantic emoji
- **Accessibility**: Color + text label (redundant encoding for colorblind users)
- **Matches user request** (#41 explicitly mentions "green, red, blue")

**Exception**: Dangerous commands keep `⚠️ Confirm` (warning triangle, not green circle)

**Alternative considered**: Keep semantic emoji (✅ ❌ 🔓)
- ❌ Rejected: Does not address visual distinction issue

**Alternative considered**: Use filled squares (🟩 🟥 🟦)
- ❌ Rejected: Less visually distinct than circles at small size

#### 11. Optional Cleanup: Remove Unused `style` Attribute

**Decision**: Remove `'style'` from all button definitions (code cleanup)

**Rationale**:
- Not functional (stripped before sending to Telegram)
- Misleading (implies Telegram supports colors)
- Adds 20 characters per button (code noise)

**Risk**: Low (attribute already ignored, removal has no runtime impact)

---

## Architecture Diagrams

### New Presigned URL Workflow (#126)

```
┌─────────┐  Step 1: Request presigned URLs            ┌────────────┐
│  Agent  │──────────────────────────────────────────>│  Lambda    │
│         │  {files: [{filename, content_type}]}      │  (API GW)  │
└─────────┘                                            └────────────┘
    │                                                         │
    │  ┌───────────────────────────────────────────────────┘
    │  │ Generate presigned PUT URLs
    │  │ (No DDB write, no Telegram notification)
    │  v
    │  {presigned_urls: [{url, s3_key, expires_at}]}
    │
    │  Step 2: Upload files directly to S3 (bypasses API GW)
    ├──────────────────────────────────────────────────────>┌──────────┐
    │  HTTP PUT to presigned URL                            │  S3      │
    │  (No Lambda involvement, no payload size limit)       │  Staging │
    │                                                        └──────────┘
    │
    │  Step 3: Confirm deployment
    ├─────────────────────────────────────────────────────>┌────────────┐
    │  {request_id}                                         │  Lambda    │
    │                                                       │  (API GW)  │
    │                                                       └────────────┘
    │                                                             │
    │  ┌──────────────────────────────────────────────────────┘
    │  │ Verify all files via head_object()
    │  │ Write DDB pending_approval record
    │  │ Send Telegram notification
    │  v
    │  {status: "pending_approval"}
    │
    │  Step 4: Manual approval (existing flow)
    └──────────────────────────────────────────────────────> [Telegram]
```

### Old Workflow (Current, Limited by API GW)

```
┌─────────┐  All files + metadata                      ┌────────────┐
│  Agent  │──────────────────────────────────────────>│  API GW    │
│         │  {files: [{filename, content: base64}]}   │  (6MB max) │
└─────────┘                                            └────────────┘
                                                              │
                                                              v
                                                       ┌────────────┐
                                                       │  Lambda    │
                                                       │  (proxy)   │
                                                       └────────────┘
                                                              │
                                                              v
                                                       ┌──────────┐
                                                       │  S3      │
                                                       │  Staging │
                                                       └──────────┘
```

---

## Security Analysis

### Presigned URL Security Model (#126)

**Threat Model**:
1. **Unauthorized upload**: Attacker obtains presigned URL → uploads malicious file
   - **Mitigation**: URL expires in 300s, includes `ContentType` enforcement
2. **File enumeration**: Attacker lists staging bucket contents
   - **Mitigation**: Bucket policy denies GET/LIST for non-Lambda principals
3. **File swapping**: Agent uploads different files than requested
   - **Mitigation**: Confirm step verifies filenames match (no file hash validation in MVP)

**Residual Risks** (Acceptable):
- Agent can upload arbitrary content (within expiry window)
  - **Justification**: Agent is already trusted (requests approval for commands)
- No cryptographic verification of file content (hash, signature)
  - **Justification**: Human approval is final security gate (reviews file list)

### Button Color Security (#41)

**Risk**: None (purely cosmetic change)
- No changes to callback data format (`approve:`, `deny:`, etc.)
- No functional impact on approval logic

---

## Cost Impact Analysis

### Presigned URL Cost Increase (#126)

**Current (Lambda Proxy)**:
- API GW: $3.50/million requests
- Lambda compute: ~$0.000017 per 10MB upload (512MB memory, 2s)

**New (Presigned URL)**:
- API GW (Step 1): $3.50/million requests (metadata only, < 1KB payload)
- S3 PUT: $0.005/1000 requests (30 files = $0.00015)
- API GW (Step 3): $3.50/million requests (confirm, < 1KB payload)
- **Total**: ~$0.0001585 per deployment

**Cost increase**: ~9× per deployment, but **$0.00014 absolute increase**

**Verdict**: **Negligible** (operational benefit of bypassing 6MB limit is critical)

### Button Color Cost (#41)

**Cost**: $0 (no API changes, no resource changes)

---

## Testing Strategy Summary

### Unit Tests (Automated)

**Coverage targets**:
- New tools: 90%+
- Shared helpers: 100%

**Key test cases**:
- Presigned URL generation for valid metadata
- Blocked extension rejection
- File verification (head_object success/failure)
- Button emoji updates (text starts with `🟢`, `🔴`, `🔵`)

### Integration Tests (Automated)

**End-to-end flows**:
- Full deploy: request → upload → confirm → approve
- Presigned URL expiry: request → wait 6 min → upload fails
- Partial upload: upload 28/30 files → confirm fails

### Manual Tests (Checklist)

**Pre-deployment**:
- Deploy 20MB bundle (30 files) to staging
- Verify Telegram notifications render correctly
- Test approval callback (approve/deny)
- Check CloudWatch Logs for errors

**Post-deployment**:
- Monitor Lambda errors (CloudWatch)
- Monitor Telegram notification failures
- Gather user feedback (button colors)

---

## Open Questions (Require Product Owner Decision)

### Critical (Blocks Sprint 38)

**Q1: What is the actual scope of issue #46?**
- **Options**: Buttons (0h), Message bodies (12h), Specific flows (4–6h)
- **Impact**: Determines sprint workload
- **Deadline**: Week 1 of Sprint 38

### Non-Critical (Can defer to Sprint 39)

**Q2: Should we auto-cleanup expired presigned upload files?**
- **Proposed**: S3 lifecycle rule (delete `frontend/` after 7 days)
- **Decision**: Include in Sprint 38 (low risk, high value)

**Q3: Should we remove the `style` attribute entirely?**
- **Proposed**: Remove in Sprint 39 (separate cleanup PR)
- **Decision**: Optional in Sprint 38 (low priority)

---

## Sprint 38 Success Criteria

**Functional**:
- ✅ Successfully deploy 20MB frontend bundle via presigned URL workflow
- ✅ Presigned URL generation: < 1s for 50 files
- ✅ File verification (head_object): < 2s for 50 files
- ✅ Buttons visually distinct (green/red/blue circles)

**Operational**:
- ✅ No increase in Lambda errors (CloudWatch Logs)
- ✅ No increase in Telegram notification failures
- ✅ No regressions in existing `bouncer_deploy_frontend` tool

**Documentation**:
- ✅ TOOLS.md updated with new tools + deprecation notice
- ✅ Changelog updated (Sprint 38 entry)
- ✅ All specs reviewed and approved

---

## Risks & Mitigation

### High Risk

**1. Staging bucket policy breaks existing uploads**
- **Likelihood**: Medium
- **Impact**: High (breaks `bouncer_upload` tool)
- **Mitigation**: Test existing tools before deploying policy
- **Rollback**: Remove bucket policy (15 minutes)

### Medium Risk

**2. Scope ambiguity for #46**
- **Likelihood**: High (issue title unclear)
- **Impact**: Medium (affects sprint planning)
- **Mitigation**: Clarify scope with PO in Week 1

**3. Presigned URL expiry edge cases**
- **Likelihood**: Low
- **Impact**: Medium (agent must re-request URLs)
- **Mitigation**: Extensive integration testing

### Low Risk

**4. Emoji rendering on older clients**
- **Likelihood**: Low (colored circles in Unicode 12.0, 2019)
- **Impact**: Low (text labels remain readable)
- **Mitigation**: Manual testing on iOS + Android

---

## References

### GitHub Issues
- #126: bouncer_deploy_frontend presigned URL refactor (P0)
- #46: Localize Telegram button labels → English (P1)
- #41: Inline keyboard button color (P1)

### Related Code
- `src/mcp_deploy_frontend.py` (685 lines) — current implementation
- `src/mcp_presigned.py` (612 lines) — presigned URL helpers
- `src/notifications.py` (960 lines) — Telegram notifications
- `template.yaml` — CloudFormation (IAM, S3 bucket)

### External Documentation
- [AWS S3 Presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/PresignedUrlUploadObject.html)
- [Telegram Bot API InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton)
- [WCAG 2.1 Color Accessibility](https://www.w3.org/WAI/WCAG21/Understanding/use-of-color.html)

---

## Sprint 38 File Manifest

```
specs/sprint38/
├── SUMMARY.md                                  # This file
├── spec-001-deploy-frontend-presigned.md       # Feature spec (#126)
├── spec-002-button-labels-english.md           # Feature spec (#46)
├── spec-003-button-colors.md                   # Feature spec (#41)
├── plan-001-deploy-frontend-presigned.md       # Implementation plan (#126)
└── tasks.md                                    # Sprint task breakdown
```

---

**Status**: ✅ All specs complete and ready for review

**Next Steps**:
1. Product owner review (clarify #46 scope)
2. Team review (architecture + test strategy approval)
3. Sprint kickoff (assign tasks, create branches)
