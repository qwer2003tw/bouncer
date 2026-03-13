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

# 環境變數
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
MESSAGE_THREAD_ID = os.environ.get('MESSAGE_THREAD_ID', '')
HISTORY_TABLE = os.environ.get('HISTORY_TABLE', 'bouncer-deploy-history')
LOCKS_TABLE = os.environ.get('LOCKS_TABLE', 'bouncer-deploy-locks')
ARTIFACTS_BUCKET = os.environ.get('ARTIFACTS_BUCKET', '')
DEPLOYS_TABLE = os.environ.get('DEPLOYS_TABLE', 'bouncer-deploys')

# DynamoDB
dynamodb = boto3.resource('dynamodb')
history_table = dynamodb.Table(HISTORY_TABLE)
locks_table = dynamodb.Table(LOCKS_TABLE)


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
    elif action == 'analyze':
        return handle_analyze(event)
    elif action == 'infra_approval_request':
        return handle_infra_approval_request(event)
    else:
        return {'error': f'Unknown action: {action}'}


def handle_start(event):
    """部署開始通知：更新現有審批訊息 + pin"""
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

    # 從 DDB 讀取現有的 telegram_message_id（由 callbacks.py 儲存）
    history = get_history(deploy_id)
    existing_message_id = history.get('telegram_message_id') if history else None

    if existing_message_id:
        # 更新現有訊息（審批訊息）而非新建
        update_telegram_message(int(existing_message_id), text)
        message_id = int(existing_message_id)
    else:
        # 若無現有訊息（例如直接觸發），則新建
        message_id = send_telegram_message(text)

    # 更新歷史記錄
    update_history(deploy_id, {
        'status': 'RUNNING',
        'telegram_message_id': message_id,
        'phase': 'INITIALIZING'
    })

    # NOTE: Do NOT pin here. callbacks.py already pins the message when the deploy
    # is approved (handle_deploy_callback). Pinning again in handle_start would
    # cause a double-pin regression (Issue #119).

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
        unpin_telegram_message(message_id)
    else:
        send_telegram_message(text)

    # 更新歷史
    update_history(deploy_id, {
        'status': 'SUCCESS',
        'finished_at': int(time.time()),
        'duration_seconds': duration,
        'build_id': build_id
    })

    # 釋放部署鎖
    release_lock(project_id)

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
    error_message = extract_error_message(error)

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
        unpin_telegram_message(message_id)
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

    # 釋放部署鎖
    release_lock(project_id)

    return {'status': 'failed'}


def handle_analyze(event):
    """Handle AnalyzeChangeset SFN state.

    Called directly by Step Functions as a normal Task (not waitForTaskToken).
    CodeBuild (.sync) finishes first, then SFN calls this Lambda to analyze
    the freshly packaged template stored in S3 (URL retrieved from DDB).

    Returns dict with is_code_only + metadata for CheckChangesetResult Choice state.
    On error → returns is_code_only=False (fail-safe: routes to WaitForInfraApproval).
    """
    from changeset_analyzer import (
        create_dry_run_changeset,
        analyze_changeset,
        cleanup_changeset,
        is_code_only_change,
    )

    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')

    # Get stack_name and template_s3_url from DDB deploy record
    # sam_deploy.py already calls update_template_s3_url() which stores the fresh URL
    stack_name = _get_stack_name(project_id)  # use project_id to query bouncer-projects
    template_s3_url = _get_template_s3_url(project_id)

    # Special case: bouncer-deployer updates itself — always treat as safe (infra changes are intentional)
    SELF_DEPLOYING_PROJECTS = {'bouncer-deployer'}
    if project_id in SELF_DEPLOYING_PROJECTS:
        print(f"[analyze] {project_id!r} is self-deploying — treating all changes as safe (auto-approve)")
        return {
            'is_code_only': True,
            'deploy_id': deploy_id,
            'project_id': project_id,
            'change_count': 0,
            'analysis_error': None,
        }

    if not stack_name or not template_s3_url:
        print(f"Error: Missing stack_name={stack_name!r} or template_s3_url={template_s3_url!r} for deploy_id={deploy_id}")
        # Fail-safe: return is_code_only=False so WaitForInfraApproval handles it
        return {
            'is_code_only': False,
            'deploy_id': deploy_id,
            'project_id': project_id,
            'change_count': 0,
            'analysis_error': 'Missing stack_name or template_s3_url',
        }

    cfn = boto3.client('cloudformation', region_name='us-east-1')

    changeset_name = None
    try:
        changeset_name = create_dry_run_changeset(cfn, stack_name, template_s3_url)
        analysis = analyze_changeset(cfn, stack_name, changeset_name)

        # Special case: "No updates" means stack is already at latest → treat as code-only
        if analysis.error and 'No updates are to be performed' in analysis.error:
            print(f"[analyze] No updates to stack {stack_name!r} — treating as code-only (auto-approve)")
            return {
                'is_code_only': True,
                'deploy_id': deploy_id,
                'project_id': project_id,
                'change_count': 0,
                'analysis_error': None,
            }

        is_code_only = is_code_only_change(analysis)
        return {
            'is_code_only': is_code_only,
            'deploy_id': deploy_id,
            'project_id': project_id,
            'change_count': len(analysis.resource_changes),
            'analysis_error': analysis.error,
        }

    except Exception as exc:  # noqa: BLE001 — fail-safe: route to WaitForInfraApproval
        print(f"[analyze] Changeset analysis failed: {exc}")
        return {
            'is_code_only': False,
            'deploy_id': deploy_id,
            'project_id': project_id,
            'change_count': 0,
            'analysis_error': str(exc)[:256],
        }
    finally:
        if changeset_name:
            try:
                cleanup_changeset(cfn, stack_name, changeset_name)
            except Exception:  # noqa: BLE001
                pass


def _get_template_s3_url(project_id: str) -> str:
    """Get template_s3_url from DDB projects table (set by sam_deploy.py after package)."""
    if not project_id:
        return ''
    try:
        projects_table_name = os.environ.get('PROJECTS_TABLE', 'bouncer-projects')
        ddb = boto3.resource('dynamodb', region_name='us-east-1')
        table = ddb.Table(projects_table_name)
        item = table.get_item(Key={'project_id': project_id}).get('Item', {})
        return item.get('template_s3_url', '')
    except Exception as e:  # noqa: BLE001
        print(f"Error getting template_s3_url from DDB: {e}")
        return 


def _get_stack_name(project_id: str) -> str:
    """Get stack_name from DDB projects table (bouncer-projects)."""
    if not project_id:
        return ''

    try:
        ddb = boto3.resource('dynamodb', region_name='us-east-1')
        table = ddb.Table(PROJECTS_TABLE)  # bouncer-projects — NotifierRole has GetItem
        result = table.get_item(Key={'project_id': project_id})
        item = result.get('Item', {})
        return item.get('stack_name', '')
    except Exception as e:
        print(f"Error getting stack_name from DDB: {e}")
        return ''


def handle_infra_approval_request(event):
    """Handle WaitForInfraApproval — notify Steven and wait for human approval.

    Stores task_token in DDB (history table with infra_approval_token field).
    Steven approves/denies via Telegram callback.

    Security: task_token is stored encrypted at rest in DDB (AWS-managed encryption).
    """
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    task_token = event.get('task_token', '')
    change_count = event.get('change_count', 0)

    # Get deploy details from history
    history = get_history(deploy_id)
    branch = history.get('branch', 'master') if history else 'master'

    # Store task_token in DDB with TTL (24h from now)
    ttl = int(time.time()) + 86400
    update_history(deploy_id, {
        'infra_approval_token': task_token,
        'infra_approval_token_ttl': ttl,
        'infra_approval_status': 'PENDING',
    })

    # Send Telegram notification with Approve/Deny buttons
    text = (
        f"⚠️ *Infrastructure Changes Detected*\n\n"
        f"📦 *專案：* {project_id}\n"
        f"🌿 *分支：* {branch}\n"
        f"🆔 *ID：* `{deploy_id}`\n\n"
        f"🔧 *變更數量：* {change_count}\n\n"
        f"⚡ 偵測到 infra 變更，需要人工確認才能繼續部署。"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ 批准部署", "callback_data": f"infra_approve:{deploy_id}"},
            {"text": "❌ 拒絕部署", "callback_data": f"infra_deny:{deploy_id}"},
        ]]
    }

    message_id = send_telegram_message(text, reply_markup=keyboard)

    # Update history with notification message_id
    update_history(deploy_id, {
        'infra_approval_message_id': message_id,
    })

    return {'status': 'approval_requested', 'message_id': message_id}


def send_telegram_message(text: str, reply_markup: dict = None) -> int:
    """發送 Telegram 訊息，返回 message_id。reply_markup 可傳 inline keyboard。"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return 0

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if MESSAGE_THREAD_ID:
        data['message_thread_id'] = MESSAGE_THREAD_ID
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)

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


def pin_telegram_message(message_id: int):
    """Pin Telegram 訊息"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage"
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'message_id': message_id,
        'disable_notification': True
    }

    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            method='POST'
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"Pinned message {message_id}")
    except Exception as e:
        print(f"Telegram pin error: {e}")


def unpin_telegram_message(message_id: int):
    """Unpin Telegram 訊息"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/unpinChatMessage"
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'message_id': message_id
    }

    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            method='POST'
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"Unpinned message {message_id}")
    except Exception as e:
        print(f"Telegram unpin error (ignored): {e}")


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


def release_lock(project_id: str):
    """釋放部署鎖"""
    if not project_id:
        return
    try:
        locks_table.delete_item(Key={'project_id': project_id})
        print(f"Released lock for {project_id}")
    except Exception as e:
        print(f"Error releasing lock for {project_id}: {e}")


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


def extract_error_message(error) -> str:
    """從 Step Functions 錯誤中提取可讀訊息"""
    if not error:
        return 'Unknown error'

    # 如果是字串，直接返回
    if isinstance(error, str):
        return error

    # 如果是 dict，嘗試解析
    if isinstance(error, dict):
        # Step Functions 錯誤結構
        cause = error.get('Cause', '')
        error_type = error.get('Error', '')

        # 嘗試解析 Cause（可能是 JSON 字串）
        if cause:
            try:
                cause_obj = json.loads(cause) if isinstance(cause, str) else cause

                # CodeBuild 錯誤
                if isinstance(cause_obj, dict):
                    build = cause_obj.get('Build', {})
                    if build:
                        status = build.get('BuildStatus', '')
                        phases = build.get('Phases', [])

                        # 找到失敗的 phase
                        for phase in phases:
                            if phase.get('PhaseStatus') == 'FAILED':
                                phase_type = phase.get('PhaseType', '')
                                contexts = phase.get('Contexts', [])
                                if contexts:
                                    msg = contexts[0].get('Message', '')
                                    return f"[{phase_type}] {msg}"

                        return f"Build {status}"

                    # 其他錯誤
                    return str(cause_obj)[:500]
            except (json.JSONDecodeError, TypeError):
                pass

            # 無法解析，返回原始 cause（截斷）
            return cause[:500] if len(cause) > 500 else cause

        # 沒有 Cause，返回 Error type
        if error_type:
            return f"Error: {error_type}"

    # 兜底
    return str(error)[:500]
