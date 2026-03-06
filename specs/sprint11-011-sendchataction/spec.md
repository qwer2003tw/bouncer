# Sprint 11-011: sendChatAction typing

> GitHub Issue: #61
> Priority: P1
> TCS: 3
> Generated: 2026-03-04

---

## Problem Statement

When a user sends an MCP tool call to Bouncer, there is no visual feedback in Telegram that Bouncer is processing the request. The bot should send a `sendChatAction` with `action=typing` to show a "typing..." indicator while processing.

### Current State

- `telegram.py`: No `sendChatAction` function exists.
- `app.py:555`: `handle_mcp_tool_call()` processes tool calls. No typing indicator sent.
- Telegram API supports `sendChatAction` with `action=typing` (lasts ~5 seconds, needs periodic re-send for long operations).

## Root Cause

Feature not implemented. `sendChatAction` was never added to the Telegram module.

## User Stories

**US-1: Processing Feedback**
As a **user who sent a command via MCP**,
I want to see a "typing..." indicator in Telegram while Bouncer processes my request,
So that I know the bot received and is working on it.

## Acceptance Criteria

1. New `send_chat_action(action='typing')` function in `telegram.py`.
2. `handle_mcp_tool_call()` sends typing action at the start of processing.
3. Long-running callbacks (deploy, upload, grant) send periodic typing actions.
4. Typing action failure does not block or fail the main operation (fire-and-forget).
5. Uses existing `TELEGRAM_CHAT_ID` for the target chat.

## Out of Scope

- Typing indicator for non-MCP requests (e.g. direct Telegram message handling).
- Custom action types (upload_photo, upload_document, etc.).
