"""
Bouncer Deployer Notifier Lambda
發送部署通知到 Telegram
"""
import json
import os
import time
import urllib.request
import urllib.parse
import boto3
from decimal import Decimal

# 環境變數
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
HISTORY_TABLE = os.environ.get('HISTORY_TABLE', 'bouncer-deploy-history')

# DynamoDB
dynamodb = boto3.resource('dynamodb')
history_table = dynamodb.Table(HISTORY_TABLE)


def lambda_handler(event, context):
    """處理通知請求"""
    action = event.get('action', '')
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    
    if action == 'start':
        return handle_start(event)
    elif action == 'progress':
        return handle_progress(event)
    elif action == 'success':
        return handle_success(event)
    elif action == 'failure':
        return handle_failure(event)
    else:
        return {'error': f'Unknown action: {action}'}


def handle_start(event):
    """部署開始通知"""
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    branch = event.get('branch', 'master')
    
    text = (
        f"⏳ *部署開始*\n\n"
        f"📦 *專案：* {project_id}\n"
        f"🌿 *分支：* {branch}\n"
        f"🆔 *ID：* `{deploy_id}`\n\n"
        f"📊 *進度：*\n"
        f"├── 🔄 初始化中...\n"
        f"├── ⏳ Template 掃描\n"
        f"├── ⏳ sam build\n"
        f"└── ⏳ sam deploy"
    )
    
    message_id = send_telegram_message(text)
    
    # 更新歷史記錄
    update_history(deploy_id, {
        'status': 'RUNNING',
        'telegram_message_id': message_id,
        'phase': 'INITIALIZING'
    })
    
    return {'message_id': message_id}


def handle_progress(event):
    """部署進度更新"""
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    branch = event.get('branch', 'master')
    phase = event.get('phase', '')
    elapsed_seconds = event.get('elapsed_seconds', 0)
    
    # 取得之前的 message_id
    history = get_history(deploy_id)
    message_id = history.get('telegram_message_id') if history else None
    
    # 根據 phase 建立進度顯示
    phases = {
        'INITIALIZING': ('🔄', '⏳', '⏳', '⏳'),
        'SCANNING': ('✅', '🔄', '⏳', '⏳'),
        'BUILDING': ('✅', '✅', '🔄', '⏳'),
        'DEPLOYING': ('✅', '✅', '✅', '🔄'),
    }
    
    icons = phases.get(phase, ('⏳', '⏳', '⏳', '⏳'))
    
    text = (
        f"⏳ *部署進行中*\n\n"
        f"📦 *專案：* {project_id}\n"
        f"🌿 *分支：* {branch}\n"
        f"🆔 *ID：* `{deploy_id}`\n\n"
        f"📊 *進度：*\n"
        f"├── {icons[0]} 初始化\n"
        f"├── {icons[1]} Template 掃描\n"
        f"├── {icons[2]} sam build\n"
        f"└── {icons[3]} sam deploy\n\n"
        f"⏱️ *已執行：* {format_duration(elapsed_seconds)}"
    )
    
    if message_id:
        update_telegram_message(message_id, text)
    else:
        message_id = send_telegram_message(text)
    
    # 更新歷史
    update_history(deploy_id, {
        'phase': phase,
        'telegram_message_id': message_id
    })
    
    return {'message_id': message_id}


def handle_success(event):
    """部署成功通知"""
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    build_id = event.get('build_id', '')
    
    # 取得歷史記錄
    history = get_history(deploy_id)
    message_id = history.get('telegram_message_id') if history else None
    started_at = history.get('started_at', 0) if history else 0
    branch = history.get('branch', 'master') if history else 'master'
    
    # 計算時間
    duration = int(time.time()) - int(started_at) if started_at else 0
    
    text = (
        f"✅ *部署成功！*\n\n"
        f"📦 *專案：* {project_id}\n"
        f"🌿 *分支：* {branch}\n"
        f"🆔 *ID：* `{deploy_id}`\n\n"
        f"📊 *進度：*\n"
        f"├── ✅ 初始化\n"
        f"├── ✅ Template 掃描\n"
        f"├── ✅ sam build\n"
        f"└── ✅ sam deploy\n\n"
        f"⏱️ *總時間：* {format_duration(duration)}"
    )
    
    if message_id:
        update_telegram_message(message_id, text)
    else:
        send_telegram_message(text)
    
    # 更新歷史
    update_history(deploy_id, {
        'status': 'SUCCESS',
        'finished_at': int(time.time()),
        'duration_seconds': duration,
        'build_id': build_id
    })
    
    return {'status': 'success'}


def handle_failure(event):
    """部署失敗通知"""
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    error = event.get('error', {})
    
    # 取得歷史記錄
    history = get_history(deploy_id)
    message_id = history.get('telegram_message_id') if history else None
    started_at = history.get('started_at', 0) if history else 0
    branch = history.get('branch', 'master') if history else 'master'
    phase = history.get('phase', 'UNKNOWN') if history else 'UNKNOWN'
    
    # 計算時間
    duration = int(time.time()) - int(started_at) if started_at else 0
    
    # 解析錯誤訊息
    error_message = 'Unknown error'
    if isinstance(error, dict):
        error_message = error.get('Cause', error.get('Error', str(error)))
    else:
        error_message = str(error)
    
    # 截斷錯誤訊息
    if len(error_message) > 500:
        error_message = error_message[:500] + '...'
    
    text = (
        f"❌ *部署失敗*\n\n"
        f"📦 *專案：* {project_id}\n"
        f"🌿 *分支：* {branch}\n"
        f"🆔 *ID：* `{deploy_id}`\n\n"
        f"❗ *失敗階段：* {phase}\n"
        f"📄 *錯誤：*\n```\n{error_message}\n```\n\n"
        f"⏱️ *執行時間：* {format_duration(duration)}"
    )
    
    if message_id:
        update_telegram_message(message_id, text)
    else:
        send_telegram_message(text)
    
    # 更新歷史
    update_history(deploy_id, {
        'status': 'FAILED',
        'finished_at': int(time.time()),
        'duration_seconds': duration,
        'error_message': error_message[:1000],
        'error_phase': phase
    })
    
    return {'status': 'failed'}


def send_telegram_message(text: str) -> int:
    """發送 Telegram 訊息，返回 message_id"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return 0
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown'
    }
    
    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            return result.get('result', {}).get('message_id', 0)
    except Exception as e:
        print(f"Telegram send error: {e}")
        return 0


def update_telegram_message(message_id: int, text: str):
    """更新 Telegram 訊息"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'message_id': message_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    
    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            method='POST'
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Telegram update error: {e}")


def get_history(deploy_id: str) -> dict:
    """取得部署歷史"""
    try:
        result = history_table.get_item(Key={'deploy_id': deploy_id})
        return result.get('Item', {})
    except Exception as e:
        print(f"Error getting history: {e}")
        return {}


def update_history(deploy_id: str, updates: dict):
    """更新部署歷史"""
    try:
        update_expr = 'SET ' + ', '.join(f'#{k} = :{k}' for k in updates.keys())
        expr_names = {f'#{k}': k for k in updates.keys()}
        expr_values = {f':{k}': v for k, v in updates.items()}
        
        history_table.update_item(
            Key={'deploy_id': deploy_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values
        )
    except Exception as e:
        print(f"Error updating history: {e}")


def format_duration(seconds: int) -> str:
    """格式化時間"""
    if seconds < 60:
        return f"{seconds} 秒"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes} 分 {secs} 秒"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours} 小時 {minutes} 分"
