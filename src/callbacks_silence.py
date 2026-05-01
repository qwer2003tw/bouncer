"""
Bouncer - Silent Rules Callback Handlers

Handles Telegram callbacks for silencing auto-approved notifications.
"""

from aws_lambda_powertools import Logger
from telegram import answer_callback, update_message
from utils import response
from db import table
from silent_rules import create_rule

logger = Logger(service="bouncer")


def handle_silence_callback(data: str, callback: dict, user_id: str) -> dict:
    """Handle silence button click.

    Callback data format: {request_id}:{service}:{action}

    Args:
        data: Callback data after 'silence:' prefix
        callback: Telegram callback query dict
        user_id: User ID who clicked the button

    Returns:
        Response dict
    """
    parts = data.split(':', 2)
    if len(parts) < 3:
        logger.warning("Invalid silence callback data", extra={
            "src_module": "callbacks_silence",
            "operation": "handle_silence_callback",
            "data": data
        })
        answer_callback(callback['id'], '❌ 格式錯誤')
        return response(200, {'ok': True})

    request_id, service, action_name = parts[0], parts[1], parts[2]

    # Get source from the original request
    try:
        item = table.get_item(Key={'request_id': request_id}).get('Item')
        if not item:
            logger.warning("Request not found for silence callback", extra={
                "src_module": "callbacks_silence",
                "operation": "handle_silence_callback",
                "request_id": request_id
            })
            answer_callback(callback['id'], '❌ 請求不存在')
            return response(200, {'ok': True})

        source = item.get('source', '')

        # Create the rule
        create_rule(source, service, action_name, user_id)

        # Confirmation popup
        answer_callback(
            callback['id'],
            f'✅ 已建立靜默規則\n{source} 的 {service}:{action_name} 不再通知',
            show_alert=True
        )

        # Update message to remove silence button (keep original text)
        message_id = callback.get('message', {}).get('message_id')
        message_text = callback.get('message', {}).get('text', '')
        if message_id:
            try:
                # Keep existing buttons except silence button
                existing_markup = callback.get('message', {}).get('reply_markup', {})
                existing_buttons = existing_markup.get('inline_keyboard', [])

                # Filter out silence button
                new_buttons = []
                for row in existing_buttons:
                    filtered_row = [
                        btn for btn in row
                        if not btn.get('callback_data', '').startswith('silence:')
                    ]
                    if filtered_row:
                        new_buttons.append(filtered_row)

                new_markup = {'inline_keyboard': new_buttons} if new_buttons else None
                update_message(message_id, message_text, reply_markup=new_markup)
            except Exception as e:
                logger.warning("Failed to update message after silence", extra={
                    "src_module": "callbacks_silence",
                    "operation": "update_message",
                    "message_id": message_id,
                    "error": str(e)
                }, exc_info=True)

        logger.info("Silent rule created from callback", extra={
            "src_module": "callbacks_silence",
            "operation": "handle_silence_callback",
            "request_id": request_id,
            "source": source,
            "service": service,
            "action": action_name,
            "user_id": user_id
        })

    except Exception as e:
        logger.error("Failed to handle silence callback", extra={
            "src_module": "callbacks_silence",
            "operation": "handle_silence_callback",
            "request_id": request_id,
            "error": str(e)
        }, exc_info=True)
        answer_callback(callback['id'], '❌ 處理失敗')
        return response(500, {'error': str(e)})

    return response(200, {'ok': True})
