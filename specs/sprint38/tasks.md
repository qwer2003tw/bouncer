# Sprint 38 Task List

**Sprint Period**: 2026-03-14 to 2026-03-28 (2 weeks)
**Total Estimated Effort**: 20–24 hours

---

## Task Registration Commands

```bash
SCRIPTS=/home/ec2-user/.openclaw/workspace/skills/mission-control/scripts

# P0 — Presigned URL refactor
bash $SCRIPTS/mc-update.sh add bouncer-s38-001 "fix: bouncer_deploy_frontend presigned URL (#126)" high

# P1 — Button labels localization (scope clarification required)
bash $SCRIPTS/mc-update.sh add bouncer-s38-002 "feat: localize Telegram buttons to English (#46)" medium

# P1 — Button visual distinction
bash $SCRIPTS/mc-update.sh add bouncer-s38-003 "feat: inline button visual distinction (#41)" medium
```

---

## bouncer-s38-001: Presigned URL Refactor (P0)

**Estimated Effort**: 12–16 hours
**Branch**: `feat/deploy-frontend-presigned-s38`
**Assignee**: TBD

### Subtasks

#### 1.1 Prerequisite Refactoring (2h)
- [ ] Create `src/mcp_deploy_frontend_shared.py`
- [ ] Extract `validate_file_metadata()` (no base64 decoding)
- [ ] Extract `_get_cache_control()`, `_get_content_type()`, `_has_blocked_extension()`
- [ ] Update `src/mcp_deploy_frontend.py` imports
- [ ] Run existing tests to verify no regressions

**Acceptance**: All existing tests pass, no functional changes

#### 1.2 Implement `bouncer_request_frontend_presigned` (4h)
- [ ] Create `src/mcp_deploy_frontend_presigned.py`
- [ ] Implement `mcp_tool_request_frontend_presigned()`
- [ ] Validate file metadata (no base64 content)
- [ ] Generate presigned PUT URLs (300s expiry)
- [ ] Return presigned URLs array (no DDB write, no notification)
- [ ] Write unit tests (90%+ coverage)

**Acceptance**: Tool returns presigned URLs for valid metadata, rejects blocked extensions

#### 1.3 Implement `bouncer_confirm_frontend_deploy` (4h)
- [ ] Create `src/mcp_deploy_frontend_confirm.py`
- [ ] Implement `mcp_tool_confirm_frontend_deploy()`
- [ ] Verify all files using `head_object()`
- [ ] Write DynamoDB `pending_approval` record
- [ ] Send Telegram notification
- [ ] Handle missing files (return error with file list)
- [ ] Write unit tests (90%+ coverage)

**Acceptance**: Confirm step verifies uploads, triggers approval flow

#### 1.4 Deprecate Old Tool (1h)
- [ ] Update `TOOLS.md`: Add deprecation warning to `bouncer_deploy_frontend`
- [ ] Add new section for presigned URL workflow
- [ ] Update `src/mcp_deploy_frontend.py` response: Add `"_warning"`

**Acceptance**: TOOLS.md reflects deprecation, old tool still functional

#### 1.5 Security — Staging Bucket Policy (2h)
- [ ] Update `template.yaml`: Add `StagingBucket` resource
- [ ] Add bucket policy (DenyUnauthorizedGET)
- [ ] Add S3 lifecycle rule (cleanup `frontend/` prefix after 7 days)
- [ ] Test presigned URLs (PUT allowed, GET denied)
- [ ] Verify existing `bouncer_upload` tool unaffected

**Acceptance**: Bucket policy restricts GET/LIST, presigned PUT still works

#### 1.6 Integration Testing (2h)
- [ ] Write `tests/integration/test_frontend_presigned_e2e.py`
- [ ] Test: Full deploy (request → upload → confirm → approve)
- [ ] Test: Presigned URL expiry (wait 6 minutes → upload fails)
- [ ] Test: Partial upload (upload 28/30 files → confirm fails)
- [ ] Manual test: Deploy 20MB bundle (30 files) to staging

**Acceptance**: All integration tests pass, manual deploy successful

#### 1.7 Deployment & Verification (1h)
- [ ] Deploy to staging environment via SAM
- [ ] Test with real frontend bundle (ztp-files)
- [ ] Verify Telegram notifications
- [ ] Check CloudWatch Logs for errors
- [ ] Deploy to production

**Acceptance**: Production deployment successful, no errors

---

## bouncer-s38-002: Button Labels Localization (P1)

**Estimated Effort**: 0–12 hours (depends on scope clarification)
**Branch**: `feat/button-labels-english-s38`
**Assignee**: TBD

### Subtasks

#### 2.1 Scope Verification (1h)
- [ ] Audit all Telegram files for Chinese text:
  ```bash
  grep -rn "[\u4e00-\u9fff]" src/ --include="*.py" | grep -i "text\|button\|keyboard"
  ```
- [ ] Review GitHub issue #46 comments
- [ ] Confirm scope with product owner:
  - **Buttons only** → Close as resolved (0h remaining)
  - **Message bodies** → Proceed to 2.2–2.4 (12h remaining)

**Acceptance**: Scope clarified, task priority/estimate adjusted

#### 2.2 Language File Setup (2h) — _If scope = message bodies_
- [ ] Create `src/i18n/en.py` (English strings dictionary)
- [ ] Define all message label keys (e.g., `SOURCE`, `ACCOUNT`, `COMMAND`)
- [ ] Add config flag: `NOTIFICATION_LANGUAGE=en` (env var)

**Acceptance**: Language file covers all notification strings

#### 2.3 Refactor Notifications (6h) — _If scope = message bodies_
- [ ] Update `src/notifications.py`: Replace Chinese strings with `i18n.en.*` references
- [ ] Update all notification functions (10+ functions)
- [ ] Verify button labels remain English (no changes)
- [ ] Run unit tests

**Acceptance**: All notifications use language strings, tests pass

#### 2.4 Testing & Verification (3h) — _If scope = message bodies_
- [ ] Write `tests/test_notifications_i18n.py`
- [ ] Test: English notification labels
- [ ] Test: Button labels unchanged
- [ ] Manual test: Send notification to Telegram → verify English text

**Acceptance**: All text is English, no layout issues

---

## bouncer-s38-003: Button Visual Distinction (P1)

**Estimated Effort**: 2–3 hours
**Branch**: `feat/button-colors-s38`
**Assignee**: TBD

### Subtasks

#### 3.1 Update Button Emoji (1h)
- [ ] Update `src/notifications.py`:
  - Replace `✅ Approve` → `🟢 Approve`
  - Replace `❌ Reject` → `🔴 Reject`
  - Replace `🔓 Trust` → `🔵 Trust`
  - Keep `⚠️ Confirm` (dangerous commands)
  - Keep `🛑 End Trust` / `🛑 Revoke Grant` (stop sign already red)
- [ ] Run unit tests to verify no functional regressions

**Acceptance**: All button emoji updated, tests pass

#### 3.2 Remove `style` Attribute (Optional, 0.5h)
- [ ] Remove unused `'style'` attribute from all button definitions
- [ ] Verify `_strip_unsupported_button_fields()` still works (no-op now)

**Acceptance**: Code cleanup complete, no functional changes

#### 3.3 Testing & Verification (1h)
- [ ] Write `tests/test_notifications_button_colors.py`
- [ ] Test: Approve button has green circle `🟢`
- [ ] Test: Reject button has red circle `🔴`
- [ ] Test: Trust button has blue circle `🔵`
- [ ] Test: Dangerous command uses warning triangle `⚠️`
- [ ] Manual test: Send notification to Telegram (iOS + Android)
- [ ] Verify: Colored circles render correctly in light/dark mode

**Acceptance**: All tests pass, buttons visually distinct

#### 3.4 Documentation Update (0.5h)
- [ ] Update changelog: Add Sprint 38 entry
- [ ] Verify TOOLS.md (no changes needed — internal implementation)

**Acceptance**: Changelog reflects button emoji changes

---

## Sprint Milestones

### Week 1 (2026-03-14 to 2026-03-21)
- [ ] Complete bouncer-s38-001 (Phases 0-3)
- [ ] Complete bouncer-s38-003 (button colors)
- [ ] Begin bouncer-s38-002 (scope clarification)

### Week 2 (2026-03-22 to 2026-03-28)
- [ ] Complete bouncer-s38-001 (Phases 4-5, deployment)
- [ ] Complete bouncer-s38-002 (if scope confirmed as message bodies)
- [ ] Sprint retrospective

---

## Definition of Done (Sprint-Level)

- ✅ All P0 tasks completed and deployed to production
- ✅ All P1 tasks completed (or scope clarified and backlog adjusted)
- ✅ All unit tests pass (90%+ coverage for new code)
- ✅ All integration tests pass
- ✅ Manual testing completed (Telegram notifications verified)
- ✅ TOOLS.md updated with new tools and deprecation notices
- ✅ Changelog updated
- ✅ No regressions in existing functionality
- ✅ No critical bugs reported in staging environment

---

## Risk Assessment

### High Risk Items

**1. Staging bucket policy breaking existing uploads** (bouncer-s38-001, Phase 4)
- **Mitigation**: Test existing `bouncer_upload` tool before deploying policy
- **Rollback**: Remove bucket policy, revert to IAM-only access

**2. Scope ambiguity for #46** (bouncer-s38-002)
- **Mitigation**: Clarify scope with product owner in Week 1
- **Impact**: If scope = message bodies, estimate increases from 0h → 12h

### Medium Risk Items

**3. Presigned URL expiry edge cases** (bouncer-s38-001)
- **Mitigation**: Extensive integration testing (Phase 5)
- **Fallback**: Agent can re-request presigned URLs (generates new request_id)

**4. Emoji rendering on older Telegram clients** (bouncer-s38-003)
- **Mitigation**: Manual testing on iOS + Android
- **Fallback**: Text labels remain readable even if emoji doesn't render

---

## Dependencies

**External**:
- None (all work self-contained)

**Internal**:
- bouncer-s38-001 must complete Phases 0-3 before Phase 4 (bucket policy)
- bouncer-s38-002 scope clarification blocks 2.2–2.4

---

## Success Metrics

**Functional**:
- Successfully deploy 20MB frontend bundle via presigned URL workflow
- Presigned URL generation: < 1s for 50 files
- File verification (head_object): < 2s for 50 files

**Operational**:
- No increase in Lambda errors (CloudWatch Logs)
- No increase in Telegram notification failures

**User Experience**:
- Button colors improve approval decision speed (qualitative feedback)
- No confusion from deprecated tool (no support tickets)

---

## Post-Sprint Actions

**Sprint 39**:
- Monitor usage of new presigned URL tools (CloudWatch metrics)
- Gather feedback from ztp-files agent maintainers
- Plan sunset of old `bouncer_deploy_frontend` tool (Sprint 40)

**Sprint 40**:
- Remove old tool from TOOLS.md (breaking change announcement)
- Update all agent codebases to use new workflow

---

## References

- **Specs**: `specs/sprint38/spec-001-*.md`, `spec-002-*.md`, `spec-003-*.md`
- **Plan**: `specs/sprint38/plan-001-deploy-frontend-presigned.md`
- **GitHub Issues**: #126, #46, #41
