"""
Bouncer - 輸出分頁模組
處理長輸出的分頁存儲和取得
"""
import time
import boto3


from constants import TABLE_NAME, OUTPUT_MAX_INLINE, OUTPUT_PAGE_SIZE, OUTPUT_PAGE_TTL
from telegram import send_telegram_message_silent

__all__ = [
    'store_paged_output',
    'get_paged_output',
    'send_remaining_pages',
]

# DynamoDB - lazy init
_table = None


def _get_table():
    global _table
    if _table is None:
        dynamodb = boto3.resource('dynamodb')
        _table = dynamodb.Table(TABLE_NAME)
    return _table


def send_remaining_pages(request_id: str, total_pages: int):
    """自動發送剩餘的分頁內容"""
    if total_pages <= 1:
        return

    for page_num in range(2, total_pages + 1):
        page_id = f"{request_id}:page:{page_num}"
        try:
            result = _get_table().get_item(Key={'request_id': page_id}).get('Item')
            if result and 'content' in result:
                content = result['content']
                send_telegram_message_silent(
                    f"📄 *第 {page_num}/{total_pages} 頁*\n\n"
                    f"```\n{content}\n```"
                )
        except Exception as e:
            print(f"Error sending page {page_num}: {e}")


def store_paged_output(request_id: str, output: str) -> dict:
    """存儲長輸出並分頁

    Returns:
        dict with page info and first page content
    """
    if len(output) <= OUTPUT_MAX_INLINE:
        return {'paged': False, 'result': output}

    # 分頁
    chunks = [output[i:i+OUTPUT_PAGE_SIZE] for i in range(0, len(output), OUTPUT_PAGE_SIZE)]
    total_pages = len(chunks)
    ttl = int(time.time()) + OUTPUT_PAGE_TTL

    # 存儲每一頁（跳過第一頁，會直接回傳）
    for i, chunk in enumerate(chunks[1:], start=2):
        _get_table().put_item(Item={
            'request_id': f"{request_id}:page:{i}",
            'content': chunk,
            'page': i,
            'total_pages': total_pages,
            'original_request': request_id,
            'ttl': ttl
        })

    return {
        'paged': True,
        'result': chunks[0],
        'page': 1,
        'total_pages': total_pages,
        'output_length': len(output),
        'next_page': f"{request_id}:page:2" if total_pages > 1 else None
    }


def get_paged_output(page_request_id: str) -> dict:
    """取得分頁輸出"""
    try:
        result = _get_table().get_item(Key={'request_id': page_request_id})
        item = result.get('Item')

        if not item:
            return {'error': '分頁不存在或已過期'}

        page = int(item.get('page', 0))
        total_pages = int(item.get('total_pages', 0))

        return {
            'result': item.get('content', ''),
            'page': page,
            'total_pages': total_pages,
            'next_page': f"{item.get('original_request')}:page:{page+1}" if page < total_pages else None
        }
    except Exception as e:
        return {'error': f'取得分頁失敗: {str(e)}'}
