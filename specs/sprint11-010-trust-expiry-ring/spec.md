# Sprint 11-010: trust expiry + pending 應響鈴

> GitHub Issue: #65
> Priority: P1
> TCS: 2
> Generated: 2026-03-04

---

## Problem Statement

`_send_trust_expiry_notification()` (`app.py:331`) **always** uses `send_telegram_message_silent()` — even when there are pending requests requiring manual approval.

When trust expires and `pending_count > 0`, the user needs to act immediately (approve/reject pending requests). A silent notification is easily missed, causing pending requests to time out.

### Current State

- `_send_trust_expiry_notification()` (`app.py:353-390`): Builds text with pending request details, always sends via `send_telegram_message_silent()`.
- `handle_trust_expiry()` (`app.py:202`): Queries pending requests, calls `_send_trust_expiry_notification()` with `pending_count`.
- `send_telegram_message_silent()` (`telegram.py:162`): Sets `disable_notification: True`.
- `send_telegram_message()` (`telegram.py:146`): Normal notification (rings).

## Root Cause

`_send_trust_expiry_notification()` was implemented with `send_telegram_message_silent()` unconditionally — no distinction between "all clear" (0 pending) and "action required" (>0 pending).

## User Stories

**US-1: Action-Required Ring**
As a **user with pending requests**,
I want trust expiry notification to ring (not silent) when there are pending requests,
So that I notice and take action before requests time out.

## Acceptance Criteria

1. `pending_count > 0` → `send_telegram_message()` (rings).
2. `pending_count == 0` → `send_telegram_message_silent()` (silent, no action needed).
3. No change to notification text content.

## Out of Scope

- Adding buttons to re-approve pending requests from the notification.
- Changing trust expiry logic.
