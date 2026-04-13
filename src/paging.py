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
    OUTPUT_MAX_INLINE,
    OUTPUT_PAGE_SIZE,
    OUTPUT_PAGE_TTL,
    OUTPUT_HARD_CAP_BYTES,
    OUTPUT_TRUNCATION_NOTICE_TEMPLATE,
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
    paged        : True when output was split into multiple pages
    result       : First-page content (always present)
    page         : Page number of *result* (always 1)
    total_pages  : Total number of pages (1 when not paged)
    output_length: Length of the *original* output before any truncation
    next_page    : DynamoDB key for the next page, or None
    truncated    : True when the original output was capped at OUTPUT_HARD_CAP_BYTES
    """
    paged: bool
    result: str
    page: int = 1
    total_pages: int = 1
    output_length: int = 0
    next_page: Optional[str] = None
    truncated: bool = False

    def to_dict(self) -> dict:
        """Backwards-compatible dict for callers that use dict-access."""
        d: dict = {
            'paged': self.paged,
            'result': self.result,
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
    """Store long output with pagination.

    Strategy
    --------
    1. Output ≤ OUTPUT_MAX_INLINE  → return as-is (no DynamoDB writes)
    2. Output ≤ OUTPUT_HARD_CAP_BYTES → chunk into OUTPUT_PAGE_SIZE pages,
       write pages 2..N to DynamoDB, return page-1 metadata
    3. Output > OUTPUT_HARD_CAP_BYTES → cap + append truncation notice,
       then apply same chunking as step 2

    DynamoDB item size is kept well under 400 KB because each page is at most
    OUTPUT_PAGE_SIZE characters (default 4 000 chars ≈ 4 KB UTF-8 worst-case,
    far below the 400 KB per-item limit).
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

    # --- Step 2: check inline threshold ---
    if len(output) <= OUTPUT_MAX_INLINE:
        return PaginatedOutput(
            paged=False,
            result=output,
            page=1,
            total_pages=1,
            output_length=original_length,
            truncated=truncated,
        )

    # --- Step 3: chunk ---
    chunks = _split_chunks(output, OUTPUT_PAGE_SIZE)
    total_pages = len(chunks)
    ttl = int(time.time()) + OUTPUT_PAGE_TTL

    # Write pages 2..N (page 1 is returned inline)
    _write_pages(request_id, chunks, total_pages, ttl)

    next_page_id = f"{request_id}:page:2" if total_pages > 1 else None

    return PaginatedOutput(
        paged=True,
        result=chunks[0],
        page=1,
        total_pages=total_pages,
        output_length=original_length,
        next_page=next_page_id,
        truncated=truncated,
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
        logger.error("get_paged_output error: %s", e, extra={"src_module": "paging", "operation": "get_paged_output", "error": str(e)})
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
            logger.error("send_remaining_pages error on page %d: %s", page_num, e, extra={"src_module": "paging", "operation": "send_remaining_pages", "page_num": page_num, "error": str(e)})
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
    """Write pages 2..N to DynamoDB (page 1 is returned inline, never stored).

    Deprecated: use _write_all_pages instead.
    """
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
    """Write ALL pages (1..N) to DynamoDB.

    Page 1 is also written so that show_page callback can read it
    without special-casing (fixes PR #276 regression).
    """
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
