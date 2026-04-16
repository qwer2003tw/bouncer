"""
Bouncer - 輸出分頁模組 (v2 — Aggressive Refactor)

設計原則：
  - PaginatedOutput dataclass 作為核心抽象
  - 硬性上限 (OUTPUT_HARD_CAP_BYTES) 防止 DynamoDB 400KB 炸掉
  - store_paged_output() 永遠不截斷前端可見資料，只加截斷通知
  - 所有分頁 metadata 集中在一個 dataclass，不再散落各處
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from botocore.exceptions import ClientError

from aws_lambda_powertools import Logger
import db as _db

from constants import (
    OUTPUT_PAGE_TTL,
    OUTPUT_HARD_CAP_BYTES,
    OUTPUT_TRUNCATION_NOTICE_TEMPLATE,
    TELEGRAM_PAGE_SIZE,
)
from telegram import send_telegram_message_silent

logger = Logger(service="bouncer")

__all__ = [
    'PaginatedOutput',
    'store_paged_output',
    'get_paged_output',
    'send_remaining_pages',
]

# DynamoDB - via db.py (lazy init)
# Tests may inject directly: paging._table = moto_table
_table = None


def _get_table():
    """Get table, with test override support. Unified fallback via db.table."""
    if _table is not None:
        return _table
    return _db.table


# ---------------------------------------------------------------------------
# Core abstraction
# ---------------------------------------------------------------------------

@dataclass
class PaginatedOutput:
    """Immutable value-object describing a (possibly paginated) command output.

    Fields
    ------
    paged        : Always False (MCP never paged, returns full result)
    result       : Full command output (always present)
    page         : Always 1
    total_pages  : Always 1 (MCP doesn't page)
    output_length: Length of the *original* output before any truncation
    next_page    : Always None (MCP doesn't page)
    truncated    : True when the original output was capped at OUTPUT_HARD_CAP_BYTES
    telegram_pages: Number of Telegram pages stored in DDB for show_page callback
    """
    paged: bool
    result: str
    page: int = 1
    total_pages: int = 1
    output_length: int = 0
    next_page: Optional[str] = None
    truncated: bool = False
    telegram_pages: int = 1  # New field: number of Telegram pages in DDB

    def to_dict(self) -> dict:
        """Backwards-compatible dict for callers that use dict-access."""
        d: dict = {
            'paged': self.paged,
            'result': self.result,
            'telegram_pages': self.telegram_pages,
        }
        if self.paged:
            d.update({
                'page': self.page,
                'total_pages': self.total_pages,
                'output_length': self.output_length,
                'next_page': self.next_page,
                'truncated': self.truncated,
            })
        return d

    # Allow dict-style access so existing callers don't need changes
    def __getitem__(self, key: str):
        return self.to_dict()[key]

    def get(self, key: str, default=None):
        return self.to_dict().get(key, default)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def store_paged_output(request_id: str, output: str) -> PaginatedOutput:
    """Store long output with Telegram pagination.

    Strategy (Sprint 83: Unified Paging)
    --------
    1. MCP always returns full result (no pagination)
    2. Telegram pages are stored separately in DDB at TELEGRAM_PAGE_SIZE chunks
    3. Output > OUTPUT_HARD_CAP_BYTES → cap + append truncation notice

    This eliminates the gap between MCP pagination (was 4000) and Telegram
    truncation (was 3500), ensuring users can see all output via show_page.
    """
    truncated = False
    original_length = len(output)

    # --- Step 1: hard cap ---
    if len(output) > OUTPUT_HARD_CAP_BYTES:
        truncated = True
        notice = OUTPUT_TRUNCATION_NOTICE_TEMPLATE.format(
            original_length=original_length,
            cap=OUTPUT_HARD_CAP_BYTES,
        )
        output = output[:OUTPUT_HARD_CAP_BYTES] + "\n" + notice

    # --- Step 2: Store Telegram pages for show_page callback ---
    # Chunk at TELEGRAM_PAGE_SIZE (3800 chars) so Telegram buttons can navigate
    tg_chunks = _split_chunks(output, TELEGRAM_PAGE_SIZE)
    tg_total = len(tg_chunks)

    if tg_total > 1:
        ttl = int(time.time()) + OUTPUT_PAGE_TTL
        _write_all_pages(request_id, tg_chunks, tg_total, ttl)

    # --- Step 3: Return full result (MCP never paged) ---
    return PaginatedOutput(
        paged=False,  # MCP never paged
        result=output,  # full result for MCP
        page=1,
        total_pages=1,
        output_length=original_length,
        truncated=truncated,
        telegram_pages=tg_total,  # new field for Telegram button logic
    )


def get_paged_output(page_request_id: str) -> dict:
    """Retrieve a page from DynamoDB.

    Returns a dict with keys: result, page, total_pages, next_page
    or: error (str) on failure.
    """
    try:
        result = _get_table().get_item(Key={'request_id': page_request_id})
        item = result.get('Item')

        if not item:
            return {'error': '分頁不存在或已過期'}

        page = int(item.get('page', 0))
        total_pages = int(item.get('total_pages', 0))
        original_request = item.get('original_request', '')

        next_page = (
            f"{original_request}:page:{page + 1}"
            if page < total_pages
            else None
        )

        return {
            'result': item.get('content', ''),
            'page': page,
            'total_pages': total_pages,
            'next_page': next_page,
        }
    except ClientError as e:
        logger.exception("get_paged_output error: %s", e, extra={"src_module": "paging", "operation": "get_paged_output", "error": str(e)})
        return {'error': f'取得分頁失敗: {str(e)}'}


def send_remaining_pages(request_id: str, total_pages: int) -> None:
    """Auto-send pages 2..total_pages to Telegram.

    Fetches each page from DynamoDB and sends it as a silent message.
    Errors per-page are logged but do not abort the remaining pages.
    """
    if total_pages <= 1:
        return

    for page_num in range(2, total_pages + 1):
        page_id = f"{request_id}:page:{page_num}"
        try:
            item = _get_table().get_item(Key={'request_id': page_id}).get('Item')
            if item and 'content' in item:
                content = item['content']
                send_telegram_message_silent(
                    f"📄 *第 {page_num}/{total_pages} 頁*\n\n"
                    f"```\n{content}\n```"
                )
            else:
                logger.warning("send_remaining_pages: page %s not found in DynamoDB", page_id, extra={"src_module": "paging", "operation": "send_remaining_pages", "page_id": page_id})
        except (ClientError, OSError, TimeoutError, ConnectionError) as e:
            logger.exception("send_remaining_pages error on page %d: %s", page_num, e, extra={"src_module": "paging", "operation": "send_remaining_pages", "page_num": page_num, "error": str(e)})
            # Continue sending remaining pages even if one fails


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _split_chunks(text: str, chunk_size: int) -> list[str]:
    """Split *text* into chunks of at most *chunk_size* characters."""
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


def _write_pages(
    request_id: str,
    chunks: list[str],
    total_pages: int,
    ttl: int,
) -> None:
    """Write pages 2..N to DynamoDB (deprecated, use _write_all_pages)."""
    table = _get_table()
    for i, chunk in enumerate(chunks[1:], start=2):
        table.put_item(Item={
            'request_id': f"{request_id}:page:{i}",
            'content': chunk,
            'page': i,
            'total_pages': total_pages,
            'original_request': request_id,
            'ttl': ttl,
        })


def _write_all_pages(
    request_id: str,
    chunks: list[str],
    total_pages: int,
    ttl: int,
) -> None:
    """Write ALL pages (1..N) to DynamoDB so show_page can read any page."""
    table = _get_table()
    for i, chunk in enumerate(chunks, start=1):
        table.put_item(Item={
            'request_id': f"{request_id}:page:{i}",
            'content': chunk,
            'page': i,
            'total_pages': total_pages,
            'original_request': request_id,
            'ttl': ttl,
        })
