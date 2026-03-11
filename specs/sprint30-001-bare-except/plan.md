# Implementation Plan

## Exception Replacement Reference

| Operation | Replace `except Exception` with |
|-----------|--------------------------------|
| DynamoDB (get/put/update/delete/query/scan) | `except ClientError` |
| STS AssumeRole | `except ClientError` |
| S3 operations | `except ClientError` |
| Telegram API (urllib) | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| JSON parse (`json.loads`) | `except (json.JSONDecodeError, ValueError)` |
| Base64 decode | `except (binascii.Error, ValueError)` |
| Regex compile/match | `except re.error` |
| botocore session model lookup | `except Exception` → keep but add `# noqa: BLE001 — botocore model API has no typed exception` |
| StepFunctions (start/stop/history) | `except ClientError` |
| CloudFormation (describe_stack_events) | `except ClientError` |
| EventBridge Scheduler | `except ClientError` |
| Secrets Manager | `except ClientError` |
| shlex.split / CLI parse | `except ValueError` |
| Top-level Lambda handler | Keep `except Exception` + `logger.exception()` + `# noqa: BLE001` |
| Metrics (emit_metric) | `except Exception` → `# noqa: BLE001 — best-effort metrics` |

---

## Batch A — 安全核心（mcp_execute.py + trust.py + app.py top-level）

### Files

| File | Lines | Count |
|------|-------|-------|
| mcp_execute.py | 162, 294, 362, 600, 839, 843, 1115, 1135, 1166, 1193 | 10 |
| trust.py | 207, 271, 293, 297, 334, 418, 456, 575 | 8 |
| app.py | 110, 120, 180, 203, 239, 265, 267, 335, 399, 472, 624, 642, 788, 791, 816, 865, 878, 916, 1001 | 19 |

### Changes per file — mcp_execute.py (10 sites)

| Line | Context | Current | Replace With |
|------|---------|---------|-------------|
| 162 | Shadow log DDB put_item | `except Exception` | `except ClientError` |
| 294 | Shadow smart approval error | `except Exception` | `except ClientError` (DDB-based) |
| 362 | Template scan runtime error | `except Exception` | `except (ValueError, TypeError, OSError)` — template scan is JSON/regex based |
| 600 | Grant session check DDB error | `except Exception` | `except ClientError` |
| 839 | Telegram notification failure | `except Exception` | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 843 | Orphan DDB cleanup (inner) | `except Exception` | `except ClientError` |
| 1115 | Grant notification send | `except Exception` | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 1135 | MCP tool grant_request top-level | `except Exception` | Keep `except Exception` + `# noqa: BLE001 — MCP tool entry point` + add `logger.exception()` |
| 1166 | MCP tool grant_status top-level | `except Exception` | Keep `except Exception` + `# noqa: BLE001 — MCP tool entry point` + add `logger.exception()` |
| 1193 | MCP tool revoke_grant top-level | `except Exception` | Keep `except Exception` + `# noqa: BLE001 — MCP tool entry point` + add `logger.exception()` |

**New imports needed:** `from botocore.exceptions import ClientError` (if not present), `import urllib.error`

### Changes per file — trust.py (8 sites)

| Line | Context | Current | Replace With |
|------|---------|---------|-------------|
| 207 | check_trust_session DDB get | `except Exception` | `except ClientError` |
| 271 | Schedule trust expiry notification | `except Exception` | `except ClientError` (EventBridge Scheduler via scheduler_service) |
| 293 | Cancel trust expiry schedule | `except Exception` | `except ClientError` |
| 297 | Revoke trust DDB update | `except Exception` | `except ClientError` |
| 334 | Increment command count DDB | `except Exception` | `except ClientError` |
| 418 | Emit metric (best-effort) | `except Exception` | `except Exception` + `# noqa: BLE001 — best-effort metrics, must not block trust validation` |
| 456 | track_command_executed DDB | `except Exception` | `except ClientError` |
| 575 | Increment upload count DDB | `except Exception` | `except ClientError` |

**New imports needed:** `from botocore.exceptions import ClientError`

### Changes per file — app.py (19 sites)

| Line | Context | Current | Replace With |
|------|---------|---------|-------------|
| 110 | Cleanup handler DDB get | `except Exception` | `except ClientError` |
| 120 | Fallback message update (Telegram) | `except Exception` | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 180 | Cleanup Telegram message update | `except Exception` | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 203 | Cleanup DDB status update | `except Exception` | `except ClientError` |
| 239 | Trust expiry DDB get | `except Exception` | `except ClientError` |
| 265 | Trust expiry mark summary_sent | `except Exception` | `except ClientError` |
| 267 | Trust expiry send_summary | `except Exception` | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError)` — calls both Telegram + DDB |
| 335 | Query pending for trust DDB | `except Exception` | `except ClientError` |
| 399 | Trust expiry Telegram notification | `except Exception` | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 472 | MCP handler JSON parse | `except Exception` | `except (json.JSONDecodeError, ValueError)` |
| 624 | Get request status DDB | `except Exception` | `except ClientError` |
| 642 | REST handler JSON parse | `except Exception` | `except (json.JSONDecodeError, ValueError)` |
| 788 | Grant expiry DDB update | `except Exception` | `except ClientError` |
| 791 | Grant expiry check outer | `except Exception` | `except ClientError` + fix `print()` → `logger.error()` |
| 816 | Telegram webhook JSON parse | `except Exception` | `except (json.JSONDecodeError, ValueError)` |
| 865 | Trust item fetch for summary | `except Exception` | `except ClientError` |
| 878 | send_trust_session_summary | `except Exception` | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError)` |
| 916 | Approval action DDB get | `except Exception` | `except ClientError` |
| 1001 | Telegram signature timestamp parse | `except Exception` | `except (ValueError, TypeError)` — parsing int/float from timestamp |

**New imports needed:** `from botocore.exceptions import ClientError`, `import urllib.error`

### Testing strategy — Batch A

- Run existing test files: `test_app.py`, `test_mcp_execute*.py`, `test_trust.py`, `test_security_sprint.py`
- Some test mocks use `side_effect=Exception(...)` — update to `side_effect=ClientError(...)` where the except now catches `ClientError`
- Verify no test changes test _assertions_ (only mock exception types)
- Run full suite to confirm no regressions

---

## Batch B — 業務邏輯（callbacks.py + grant.py + notifications.py）

### Files

| File | Lines | Count |
|------|-------|-------|
| callbacks.py | 105, 146, 421, 615, 664, 751, 879, 960, 1206, 1286, 1377, 1422, 1529 | 13 |
| grant.py | 193, 216, 314, 381, 386, 412, 481, 506, 531, 564, 604, 654, 704 | 13 |
| notifications.py | 135, 152, 193, 486, 491, 538, 555, 577, 617, 688, 718, 743, 820, 897 | 14 |

### Changes per file — callbacks.py (13 sites)

| Line | Context | Replace With |
|------|---------|-------------|
| 105 | Grant approve handler | `except ClientError` (DDB + STS operations) |
| 146 | Grant deny handler | `except ClientError` |
| 421 | Auto-execute pending error | `except (ClientError, OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 615 | Account add DDB | `except ClientError` |
| 664 | Account remove DDB | `except ClientError` |
| 751 | Store telegram_message_id | `except ClientError` |
| 879 | Batch upload S3 client init | `except ClientError` |
| 960 | Individual file upload in batch | `except ClientError` — S3 operations |
| 1206 | Deploy frontend history write | `except ClientError` |
| 1286 | Deploy frontend AssumeRole | `except ClientError` |
| 1377 | Deploy frontend file upload | `except ClientError` |
| 1422 | Deploy frontend progress update | Already handled by noqa check — verify |
| 1529 | Deploy frontend verify | Context-dependent — check |

### Changes per file — grant.py (13 sites)

| Line | Context | Replace With |
|------|---------|-------------|
| 193 | match_pattern regex/compile | `except re.error` |
| 216 | normalize_command shlex | `except ValueError` |
| 314 | create_grant_request DDB | `except ClientError` (re-raise preserved) |
| 381 | Risk scoring inner (fail-open) | `except (ValueError, TypeError, OSError)` + comment: `# fail-open: scoring error → treat as low risk` |
| 386 | Precheck outer (fail-closed) | `except (ClientError, ValueError, TypeError, OSError)` + comment: `# fail-closed: precheck error → requires_individual` |
| 412 | get_grant_session DDB | `except ClientError` |
| 481 | approve_grant DDB | `except ClientError` |
| 506 | deny_grant DDB | `except ClientError` |
| 531 | execute_grant_command | `except ClientError` |
| 564 | grant complete/expire | `except ClientError` |
| 604 | grant batch execute inner | `except ClientError` |
| 654 | grant revoke | `except ClientError` |
| 704 | grant status query | `except ClientError` |

### Changes per file — notifications.py (14 sites)

| Line | Context | Replace With |
|------|---------|-------------|
| 135 | Store telegram_message_id DDB | `except ClientError` |
| 152 | Create expiry schedule | `except ClientError` |
| 193 | Parse account display | `except (ClientError, KeyError, TypeError)` — DDB lookup + dict access |
| 486 | Post notification setup | `except ClientError` |
| 491 | Send grant notification (Telegram) | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 538 | Send grant execute notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 555 | Send grant complete notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 577 | Send blocked notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 617 | Send trust upload notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 688 | Send batch upload notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 718 | Send presigned notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 743 | Send presigned batch notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 820 | Send trust session summary | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 897 | Send deploy frontend notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |

### Testing strategy — Batch B

- Run: `test_callbacks_main.py`, `test_grant.py`, `test_notifications_main.py`
- Update mocks that use `side_effect=Exception(...)` for Telegram paths to use `side_effect=OSError(...)`
- Update mocks for DDB paths to use `side_effect=ClientError(...)`
- `test_grant.py` already tests `ValueError` raises — no changes needed there

---

## Batch C — 工具模組（deployer.py + mcp_deploy_frontend.py + mcp_upload.py）

### Files

| File | Lines | Count |
|------|-------|-------|
| deployer.py | 136, 171, 231, 237, 257, 270, 304, 341, 366, 412, 424, 440, 535, 561, 681, 740, 782, 819, 822, 1141 | 20 |
| mcp_deploy_frontend.py | 104, 221, 279, 343, 415, 513, 535, 568, 586, 631 | 10 |
| mcp_upload.py | 74, 164, 354, 394, 457, 461, 699, 823, 858, 863, 1012, 1051 | 12 |

### Changes per file — deployer.py (20 sites)

| Line | Context | Replace With |
|------|---------|-------------|
| 136 | get_git_commit_info | `except (OSError, subprocess.SubprocessError)` — git subprocess |
| 171 | Get GitHub PAT from Secrets Manager | `except ClientError` |
| 231 | Check individual secret existence | `except ClientError` |
| 237 | Preflight check outer | `except ClientError` |
| 257 | list_projects DDB scan | `except ClientError` (already has ClientError above — merge or remove duplicate) |
| 270 | get_project DDB get | `except ClientError` (same pattern — merge) |
| 304 | remove_project DDB delete | `except ClientError` (merge) |
| 341 | release_lock DDB update | `except ClientError` (merge) |
| 366 | get_lock DDB get | `except ClientError` (merge) |
| 412 | update_deploy_record DDB | `except ClientError` |
| 424 | get_deploy_record DDB | `except ClientError` (merge) |
| 440 | get_deploy_history DDB query | `except ClientError` |
| 535 | start_deploy failure | `except ClientError` — SFN + DDB |
| 561 | cancel_deploy SFN stop | `except ClientError` |
| 681 | send_deploy_failure_notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 740 | SFN get_execution_history | `except ClientError` |
| 782 | CFN describe_stack_events | `except ClientError` |
| 819 | Unpin message (Telegram) | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 822 | Get execution status outer | `except ClientError` |
| 1141 | Post notification setup | `except ClientError` |

**Note:** deployer.py lines 257, 270, 304, 341, 366, 424 already have `except ClientError:` above the bare `except Exception:`. The bare except is a _duplicate fallback_. Strategy: remove the duplicate `except Exception:` block and let the existing `except ClientError:` handle it, OR merge into a single `except ClientError:`.

### Changes per file — mcp_deploy_frontend.py (10 sites)

| Line | Context | Replace With |
|------|---------|-------------|
| 104 | DDB config lookup | `except ClientError` |
| 221 | Base64 decode | `except (binascii.Error, ValueError)` |
| 279 | Project verification DDB | `except ClientError` |
| 343 | S3 staging put_object | `except ClientError` |
| 415 | DDB cleanup after notification fail | `except ClientError` |
| 513 | S3 staging (trust path) | `except ClientError` |
| 535 | AssumeRole for deploy | `except ClientError` |
| 568 | S3 target copy | `except ClientError` |
| 586 | CloudFront invalidation | `except ClientError` |
| 631 | Telegram notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |

### Changes per file — mcp_upload.py (12 sites)

| Line | Context | Replace With |
|------|---------|-------------|
| 74 | S3 head_object verification | `except ClientError` |
| 164 | Base64 decode | `except (binascii.Error, ValueError)` |
| 354 | Trust upload execution | `except ClientError` |
| 394 | S3 staging put_object | `except ClientError` |
| 457 | Telegram notification failure | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |
| 461 | Orphan DDB cleanup (inner) | `except ClientError` |
| 699 | Base64 decode (batch) | `except (binascii.Error, ValueError)` |
| 823 | Batch trust upload | `except ClientError` |
| 858 | Batch S3 staging | `except ClientError` |
| 863 | Batch rollback cleanup (inner) | `except ClientError` |
| 1012 | Batch individual file (inner) | `except ClientError` |
| 1051 | Batch notification | `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)` |

### Testing strategy — Batch C

- Run: `test_deployer_main.py`, `test_deployer_sprint10_001.py`, `test_deployer_cfn_events_s17.py`, `test_mcp_upload_sprint9_002.py`, `test_ddb_400kb_fix.py`
- deployer.py has the most complex pattern (duplicate ClientError + Exception). Code review needed to ensure no reachability issues after merging.
- Update test mocks: `side_effect=Exception('S3 connection refused')` → `side_effect=ClientError({'Error': {'Code': 'ServiceUnavailable', 'Message': 'S3 connection refused'}}, 'PutObject')`

---

## Batch D — 輔助模組（18 files, 54 sites）

### Files

| File | Lines | Count |
|------|-------|-------|
| telegram.py | 100, 127, 281, 330, 350 | 5 |
| risk_scorer.py | 244, 315, 486, 648, 1002 | 5 |
| telegram_commands.py | 99, 132, 196, 228 | 4 |
| sequence_analyzer.py | 579, 630, 671, 757 | 4 |
| scheduler_service.py | 162, 187, 326, 352 | 4 |
| mcp_presigned.py | 77, 274, 507, 546 | 4 |
| accounts.py | 66, 83, 92, 102 | 4 |
| mcp_admin.py | 59, 144, 330 | 3 |
| mcp_history.py | 238, 293, 449 | 3 |
| commands.py | 320, 663, 714 | 3 |
| help_command.py | 120, 126, 190 | 3 |
| smart_approval.py | 93, 127 | 2 |
| paging.py | 190, 216 | 2 |
| mcp_confirm.py | 94, 127 | 2 |
| utils.py | 320, 379 | 2 |
| template_scanner.py | 97, 615 | 2 |
| rate_limit.py | 115 | 1 |
| compliance_checker.py | 310 | 1 |

### Changes summary — Batch D

**telegram.py (5):**
- L100, 127: urllib Telegram API call → `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)`
- L281: send_chat_action → same as above
- L330, 350: pin/unpin message → same as above

**risk_scorer.py (5):**
- L244: JSON file load → `except (json.JSONDecodeError, OSError)`
- L315: Rules load fallback → `except (json.JSONDecodeError, OSError, ValueError)`
- L486: CLI parse → `except (ValueError, TypeError)`
- L648: Template score → `except (re.error, ValueError, TypeError)`
- L1002: Risk evaluation outer → Keep `except Exception` + `# noqa: BLE001 — fail-closed risk evaluator, must return score for any failure`

**telegram_commands.py (4):**
- L99, 132, 196: DDB query → `except ClientError`
- L228: datetime parse → `except (ValueError, TypeError)`

**sequence_analyzer.py (4):**
- L579, 630: DDB put/query → `except ClientError`
- L671: get_recent_commands call → `except ClientError`
- L757: analyze_sequence outer → `except (ClientError, ValueError, TypeError)` — combines DDB + data processing

**scheduler_service.py (4):**
- L162, 187, 326, 352: EventBridge Scheduler create/delete → `except ClientError`

**mcp_presigned.py (4):**
- L77: S3 presigned URL gen → `except (ClientError, OSError)`
- L274, 546: Notification send → `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)`
- L507: Batch presigned URL gen → `except (ClientError, OSError)`

**accounts.py (4):**
- L66: Set bot commands (Telegram) → `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)`
- L83: DDB put_item → `except ClientError`
- L92, 102: DDB get/scan → `except ClientError`

**mcp_admin.py (3):**
- L59, 144, 330: MCP tool entry points → Keep `except Exception` + `# noqa: BLE001 — MCP tool entry point` + ensure `logger.exception()`

**mcp_history.py (3):**
- L238, 293: DDB query/scan → `except ClientError`
- L449: MCP tool entry point → Keep `except Exception` + `# noqa: BLE001 — MCP tool entry point`

**commands.py (3):**
- L320: shlex.split parse → `except ValueError`
- L663: STS assume_role → `except ClientError`
- L714: CLI execution outer → Keep `except Exception` + `# noqa: BLE001 — CLI execution catch-all for user display` + ensure `logger.exception()`

**help_command.py (3):**
- L120: botocore service model lookup → `except (botocore.exceptions.UnknownServiceError, Exception)` → Keep `except Exception` + `# noqa: BLE001 — botocore model API`
- L126: operation model lookup → same
- L190: service listing → same

**smart_approval.py (2):**
- L93: Sequence analysis inner → `except (ClientError, ValueError, TypeError)`
- L127: Risk evaluation outer → Keep `except Exception` + `# noqa: BLE001 — fail-closed approval, must return result for any failure`

**paging.py (2):**
- L190: DDB get/put for paging → `except ClientError`
- L216: DDB get + Telegram send → `except (ClientError, OSError, TimeoutError, ConnectionError, urllib.error.URLError)`

**mcp_confirm.py (2):**
- L94: S3 list_objects → `except (ClientError, OSError)`
- L127: DDB put_item → `except ClientError`

**utils.py (2):**
- L320: DDB put_item (audit log) → `except ClientError`
- L379: DDB update_item (error record) → `except ClientError`

**template_scanner.py (2):**
- L97: JSON parse → `except (json.JSONDecodeError, ValueError)`
- L615: Individual check function → Keep `except Exception` + `# noqa: BLE001 — individual scanner check, fail-open by design`

**rate_limit.py (1):**
- L115: DDB rate limit check → already has correct fail-close behavior. Change to `except ClientError` (the `raise RateLimitExceeded(...)` is the correct fail-close pattern)

**compliance_checker.py (1):**
- L310: Regex substitution → `except re.error`

### Testing strategy — Batch D

- Run full test suite after all changes
- Primary focus: `test_telegram_commands.py`, `test_risk_scorer*.py`, `test_deployer*.py`
- Most changes in Batch D are straightforward DDB → `ClientError` or Telegram → urllib error replacements
- `help_command.py` uses dynamic botocore introspection — keep `except Exception` with noqa

---

## Summary of `# noqa: BLE001` Sites (Legitimate Catch-All)

| File | Line | Reason |
|------|------|--------|
| mcp_execute.py | 1135, 1166, 1193 | MCP tool entry points — must return JSON-RPC error for any failure |
| mcp_admin.py | 59, 144, 330 | MCP tool entry points |
| mcp_history.py | 449 | MCP tool entry point |
| trust.py | 418 | Best-effort metrics emission |
| risk_scorer.py | 1002 | Fail-closed risk evaluator |
| smart_approval.py | 127 | Fail-closed approval |
| commands.py | 714 | CLI execution catch-all for user display |
| help_command.py | 120, 126, 190 | botocore model introspection API |
| template_scanner.py | 615 | Individual scanner check, fail-open by design |

**Total noqa sites: 14** (out of 173 → 159 will be narrowed to typed exceptions)
